/* Nucleo em C do roteador de perfboard: a busca A* furo a furo.
 *
 * Por que existe: o A* e o laco mais quente do programa e o que define a latencia
 * quando se arrasta uma peca na tela. O resto (parser, footprints, SVG, API) nao e
 * gargalo e continua em Python, onde e mais facil de ler e alterar.
 *
 * Por que ctypes e nao extensao CPython: assim este arquivo compila com gcc puro,
 * sem cabecalhos do Python, e a mesma fonte serve para qualquer versao/plataforma.
 * Se a biblioteca nao estiver compilada, o Python usa a implementacao propria - o
 * programa nunca deixa de funcionar por falta de compilador.
 *
 * IMPORTANTE: este arquivo tem que produzir EXATAMENTE o mesmo caminho que
 * perfboard/router.py. Ha um teste diferencial que compara os dois em milhares de
 * casos; qualquer divergencia e bug aqui.
 *
 * Compilar:
 *   gcc -O2 -shared -o perfboard.dll perfboard.c        (Windows)
 *   gcc -O2 -shared -fPIC -o perfboard.so perfboard.c   (Linux)
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifdef _WIN32
#define PB_EXPORT __declspec(dllexport)
#else
#define PB_EXPORT
#endif

#define PB_LIVRE 4          /* direcao "sem direcao": veio de via, jumper ou inicio */
#define PB_DIRS 4
#define PB_ESTADOS_POR_FURO 10   /* 2 faces * 5 direcoes */

/* tipos de passo, iguais aos do Python */
#define PB_INICIO 0
#define PB_TRACE 1
#define PB_TRACE_TOP 2
#define PB_JUMPER 3
#define PB_VIA 4
#define PB_LEAD 5

static const int DC[PB_DIRS] = {1, -1, 0, 0};
static const int DR[PB_DIRS] = {0, 0, 1, -1};

typedef struct {
    int cols, rows, faces;
    double trace_cost, top_trace_cost, turn_cost, via_cost;
    double jumper_base, jumper_per_hole;
    int max_jumper, allow_jumpers;
    double pres_weight, pres;
    int soft;
} PbConfig;

/* ---------------------------------------------------------------- heap binario */

/* Ordena por (f, g, no), exatamente como o heapq do Python, que empurra a tupla
 * (f, g, no, tipo) e compara elemento a elemento.
 *
 * Os tres niveis importam:
 *   f  - o criterio de verdade do A*;
 *   g  - empate em f vai para quem ja percorreu caminho real, nao para quem so
 *        parece promissor;
 *   no - empate em f e g precisa de uma ordem TOTAL e igual a do Python, senao as
 *        duas implementacoes divergem em placa congestionada.
 *
 * `ordem` e o no na mesma ordenacao da tupla do Python: (coluna, linha, face,
 * direcao). A codificacao interna do estado usa linha antes de coluna, entao nao
 * serve para desempatar - foi essa diferenca que fez o C fechar menos redes. */
typedef struct {
    double *chave;   /* f = g + h */
    double *g;
    int *ordem;
    int *estado;
    int n, cap;
} Heap;

static int heap_init(Heap *h, int cap) {
    h->chave = (double *)malloc(sizeof(double) * cap);
    h->g = (double *)malloc(sizeof(double) * cap);
    h->ordem = (int *)malloc(sizeof(int) * cap);
    h->estado = (int *)malloc(sizeof(int) * cap);
    h->n = 0;
    h->cap = cap;
    return h->chave && h->g && h->ordem && h->estado;
}

static void heap_free(Heap *h) {
    free(h->chave);
    free(h->g);
    free(h->ordem);
    free(h->estado);
}

static inline int heap_menor(const Heap *h, int a, int b) {
    if (h->chave[a] < h->chave[b]) return 1;
    if (h->chave[a] > h->chave[b]) return 0;
    if (h->g[a] < h->g[b]) return 1;
    if (h->g[a] > h->g[b]) return 0;
    return h->ordem[a] < h->ordem[b];
}

static inline void heap_troca(Heap *h, int a, int b) {
    double k = h->chave[a]; h->chave[a] = h->chave[b]; h->chave[b] = k;
    double gg = h->g[a];    h->g[a] = h->g[b];         h->g[b] = gg;
    int o = h->ordem[a];    h->ordem[a] = h->ordem[b]; h->ordem[b] = o;
    int e = h->estado[a];   h->estado[a] = h->estado[b]; h->estado[b] = e;
}

static int heap_push(Heap *h, double chave, double g, int ordem, int estado) {
    if (h->n == h->cap) {
        int novo = h->cap * 2;
        double *k = (double *)realloc(h->chave, sizeof(double) * novo);
        double *gg = (double *)realloc(h->g, sizeof(double) * novo);
        int *o = (int *)realloc(h->ordem, sizeof(int) * novo);
        int *e = (int *)realloc(h->estado, sizeof(int) * novo);
        if (k) h->chave = k;
        if (gg) h->g = gg;
        if (o) h->ordem = o;
        if (e) h->estado = e;
        if (!k || !gg || !o || !e) return 0;
        h->cap = novo;
    }
    int i = h->n++;
    h->chave[i] = chave;
    h->g[i] = g;
    h->ordem[i] = ordem;
    h->estado[i] = estado;
    while (i > 0) {
        int pai = (i - 1) / 2;
        if (!heap_menor(h, i, pai)) break;
        heap_troca(h, i, pai);
        i = pai;
    }
    return 1;
}

static int heap_pop(Heap *h, double *chave, int *estado) {
    if (h->n == 0) return 0;
    *chave = h->chave[0];
    *estado = h->estado[0];
    h->n--;
    if (h->n > 0) {
        h->chave[0] = h->chave[h->n];
        h->g[0] = h->g[h->n];
        h->ordem[0] = h->ordem[h->n];
        h->estado[0] = h->estado[h->n];
        int i = 0;
        for (;;) {
            int e = 2 * i + 1, d = 2 * i + 2, m = i;
            if (e < h->n && heap_menor(h, e, m)) m = e;
            if (d < h->n && heap_menor(h, d, m)) m = d;
            if (m == i) break;
            heap_troca(h, i, m);
            i = m;
        }
    }
    return 1;
}

/* ---------------------------------------------------------------- contexto */

typedef struct {
    const PbConfig *cfg;
    const int *pad_fixed;             /* N*2: -1 livre, senao id da rede dona */
    const int *pad_outras;            /* N*2: quantas OUTRAS redes usam a ilha */
    const int *edge_outras;           /* N*2*2: idem para arestas (eixo 0=dir, 1=baixo) */
    const float *hist_pad;            /* N*2 */
    const float *hist_edge;           /* N*2*2 */
    const unsigned char *sob_peca;    /* N: furo coberto por corpo de componente */
    const unsigned char *tem_pino;    /* N: furo ocupado por terminal */
    const unsigned char *eh_alvo;     /* N */
    const int *alvos;
    int n_alvos;
    int net;
    int N;
} Ctx;

static inline int idx_pad(const Ctx *x, int cell, int face) {
    /* placa de face unica: as duas ilhas do furo sao o mesmo ponto */
    return cell * 2 + (x->cfg->faces >= 2 ? face : 0);
}

static inline int idx_edge(const Ctx *x, int cell, int face, int eixo) {
    return (cell * 2 + (x->cfg->faces >= 2 ? face : 0)) * 2 + eixo;
}

static inline int pad_bloqueado(const Ctx *x, int cell, int face) {
    int i = idx_pad(x, cell, face);
    int dono = x->pad_fixed[i];
    if (dono != -1 && dono != x->net) return 1;
    if (!x->cfg->soft && x->pad_outras[i] > 0) return 1;
    return 0;
}

static inline double pad_extra(const Ctx *x, int cell, int face) {
    int i = idx_pad(x, cell, face);
    double c = x->hist_pad[i];
    if (x->cfg->soft) c += x->cfg->pres * x->cfg->pres_weight * (double)x->pad_outras[i];
    return c;
}

/* aresta entre `cell` e o vizinho na direcao `dir`, normalizada para o menor furo */
static inline int aresta_de(const Ctx *x, int cell, int dir, int *eixo) {
    int cols = x->cfg->cols;
    int c = cell % cols, r = cell / cols;
    switch (dir) {
        case 0: *eixo = 0; return cell;                    /* direita */
        case 1: *eixo = 0; return cell - 1;                /* esquerda */
        case 2: *eixo = 1; return cell;                    /* baixo */
        default: *eixo = 1; return cell - cols;            /* cima */
    }
    (void)c; (void)r;
}

static inline int edge_bloqueada(const Ctx *x, int cell, int dir, int face) {
    if (x->cfg->soft) return 0;
    int eixo, base = aresta_de(x, cell, dir, &eixo);
    return x->edge_outras[idx_edge(x, base, face, eixo)] > 0;
}

static inline double edge_extra(const Ctx *x, int cell, int dir, int face) {
    int eixo, base = aresta_de(x, cell, dir, &eixo);
    int i = idx_edge(x, base, face, eixo);
    double c = x->hist_edge[i];
    if (x->cfg->soft) c += x->cfg->pres * x->cfg->pres_weight * (double)x->edge_outras[i];
    return c;
}

/* ---------------------------------------------------------------- busca */

PB_EXPORT int pb_astar(const PbConfig *cfg,
                       const int *pad_fixed, const int *pad_outras, const int *edge_outras,
                       const float *hist_pad, const float *hist_edge,
                       const unsigned char *sob_peca, const unsigned char *tem_pino,
                       const unsigned char *eh_alvo, const int *alvos, int n_alvos,
                       const int *fontes, int n_fontes, int net,
                       int *saida, int max_saida)
{
    const int cols = cfg->cols, rows = cfg->rows;
    const int N = cols * rows;
    if (N <= 0 || n_alvos <= 0 || n_fontes <= 0) return -1;

    Ctx x;
    x.cfg = cfg; x.pad_fixed = pad_fixed; x.pad_outras = pad_outras;
    x.edge_outras = edge_outras; x.hist_pad = hist_pad; x.hist_edge = hist_edge;
    x.sob_peca = sob_peca; x.tem_pino = tem_pino; x.eh_alvo = eh_alvo;
    x.alvos = alvos; x.n_alvos = n_alvos; x.net = net; x.N = N;

    const int n_estados = N * PB_ESTADOS_POR_FURO;

    double *g = (double *)malloc(sizeof(double) * n_estados);
    int *veio = (int *)malloc(sizeof(int) * n_estados);
    unsigned char *tipo = (unsigned char *)malloc(n_estados);
    unsigned char *fechado = (unsigned char *)calloc(n_estados, 1);
    if (!g || !veio || !tipo || !fechado) {
        free(g); free(veio); free(tipo); free(fechado); return -2;
    }
    for (int i = 0; i < n_estados; i++) { g[i] = INFINITY; veio[i] = -1; tipo[i] = PB_INICIO; }

    /* passo mais barato por furo, para a heuristica continuar admissivel */
    double passo_min = cfg->trace_cost;
    if (cfg->faces >= 2 && cfg->top_trace_cost < passo_min) passo_min = cfg->top_trace_cost;
    if (cfg->allow_jumpers && cfg->jumper_per_hole < passo_min) passo_min = cfg->jumper_per_hole;
    if (passo_min < 0.01) passo_min = 0.01;

    /* mesma ordenacao da tupla (coluna, linha, face, direcao) do Python */
    #define ORDEM(st) ({         int _cel = (st) / PB_ESTADOS_POR_FURO, _r0 = (st) % PB_ESTADOS_POR_FURO;         ((((_cel % cols) * rows + (_cel / cols)) * 2 + _r0 / 5) * 5 + _r0 % 5); })

    Heap h;
    if (!heap_init(&h, 1024)) {
        free(g); free(veio); free(tipo); free(fechado); return -2;
    }

    #define H(cell) ({ \
        int _c = (cell) % cols, _r = (cell) / cols, _m = 1 << 30; \
        for (int _i = 0; _i < n_alvos; _i++) { \
            int _gc = alvos[_i] % cols, _gr = alvos[_i] / cols; \
            int _d = abs(_c - _gc) + abs(_r - _gr); \
            if (_d < _m) _m = _d; \
        } \
        passo_min * (double)_m; })

    for (int i = 0; i < n_fontes; i++) {
        int st = fontes[i];
        if (st < 0 || st >= n_estados) continue;
        g[st] = 0.0;
        veio[st] = -1;
        tipo[st] = PB_INICIO;
        heap_push(&h, H(st / PB_ESTADOS_POR_FURO), 0.0, ORDEM(st), st);
    }

    int achado = -1;
    double chave;
    int st;
    while (heap_pop(&h, &chave, &st)) {
        if (fechado[st]) continue;
        fechado[st] = 1;

        int cell = st / PB_ESTADOS_POR_FURO;
        int resto = st % PB_ESTADOS_POR_FURO;
        int face = resto / 5;
        int veio_dir = resto % 5;

        if (eh_alvo[cell]) { achado = st; break; }

        int c = cell % cols, r = cell / cols;
        double gcur = g[st];

        /* 1) trilha, furo a furo na mesma face */
        if (face == 0 || cfg->faces >= 2) {
            double passo = (face == 0) ? cfg->trace_cost : cfg->top_trace_cost;
            int kind = (face == 0) ? PB_TRACE : PB_TRACE_TOP;
            for (int d = 0; d < PB_DIRS; d++) {
                int nc = c + DC[d], nr = r + DR[d];
                if (nc < 0 || nc >= cols || nr < 0 || nr >= rows) continue;
                int viz = nr * cols + nc;
                if (face == 1 && sob_peca[viz]) continue;
                if (pad_bloqueado(&x, viz, face)) continue;
                if (edge_bloqueada(&x, cell, d, face)) continue;

                double dobra = (veio_dir != PB_LIVRE && veio_dir != d) ? cfg->turn_cost : 0.0;
                double custo = passo + dobra + pad_extra(&x, viz, face) + edge_extra(&x, cell, d, face);
                int nst = (viz * 2 + face) * 5 + d;
                double ng = gcur + custo;
                if (ng < g[nst] - 1e-9) {
                    g[nst] = ng; veio[nst] = st; tipo[nst] = (unsigned char)kind;
                    heap_push(&h, ng + H(viz), ng, ORDEM(nst), nst);
                }
            }
        }

        /* 2) via: troca de face no mesmo furo */
        if (cfg->faces >= 2) {
            int outra = face == 0 ? 1 : 0;
            if (!pad_bloqueado(&x, cell, outra) && !(outra == 1 && sob_peca[cell])) {
                int gratis = (pad_fixed[idx_pad(&x, cell, 0)] == net) && tem_pino[cell];
                double custo = (gratis ? 0.0 : cfg->via_cost) + pad_extra(&x, cell, outra);
                int nst = (cell * 2 + outra) * 5 + PB_LIVRE;
                double ng = gcur + custo;
                if (ng < g[nst] - 1e-9) {
                    g[nst] = ng; veio[nst] = st;
                    tipo[nst] = (unsigned char)(gratis ? PB_LEAD : PB_VIA);
                    heap_push(&h, ng + H(cell), ng, ORDEM(nst), nst);
                }
            }
        }

        if (!cfg->allow_jumpers) continue;
        /* ou o furo tem o pino, ou tem a ponta do jumper - nunca os dois */
        if (tem_pino[cell]) continue;

        /* 3) jumper reto */
        for (int d = 0; d < PB_DIRS; d++) {
            for (int k = 2; k <= cfg->max_jumper; k++) {
                int nc = c + DC[d] * k, nr = r + DR[d] * k;
                if (nc < 0 || nc >= cols || nr < 0 || nr >= rows) break;
                int alvo = nr * cols + nc;
                if (tem_pino[alvo]) continue;
                if (pad_bloqueado(&x, alvo, face)) continue;
                double custo = cfg->jumper_base + cfg->jumper_per_hole * (double)k
                             + pad_extra(&x, alvo, face);
                int nst = (alvo * 2 + face) * 5 + PB_LIVRE;
                double ng = gcur + custo;
                if (ng < g[nst] - 1e-9) {
                    g[nst] = ng; veio[nst] = st; tipo[nst] = PB_JUMPER;
                    heap_push(&h, ng + H(alvo), ng, ORDEM(nst), nst);
                }
            }
        }

        /* 4) jumper direto ate a vizinhanca de um destino */
        for (int i = 0; i < n_alvos; i++) {
            int gc = alvos[i] % cols, gr = alvos[i] / cols;
            for (int d = 0; d < PB_DIRS; d++) {
                int nc = gc + DC[d], nr = gr + DR[d];
                if (nc < 0 || nc >= cols || nr < 0 || nr >= rows) continue;
                int alvo = nr * cols + nc;
                if (alvo == cell || tem_pino[alvo]) continue;
                double dx = (double)(nc - c), dy = (double)(nr - r);
                double dist = sqrt(dx * dx + dy * dy);
                if (dist < 2.0 || dist > (double)cfg->max_jumper) continue;
                if (pad_bloqueado(&x, alvo, face)) continue;
                double custo = cfg->jumper_base + cfg->jumper_per_hole * dist
                             + pad_extra(&x, alvo, face);
                int nst = (alvo * 2 + face) * 5 + PB_LIVRE;
                double ng = gcur + custo;
                if (ng < g[nst] - 1e-9) {
                    g[nst] = ng; veio[nst] = st; tipo[nst] = PB_JUMPER;
                    heap_push(&h, ng + H(alvo), ng, ORDEM(nst), nst);
                }
            }
        }
    }

    #undef H
    #undef ORDEM

    int n = -1;
    if (achado >= 0) {
        /* desenrola de tras para frente e inverte */
        int tam = 0;
        for (int cur = achado; cur != -1; cur = veio[cur]) tam++;
        if (tam * 3 <= max_saida) {
            int pos = tam - 1;
            for (int cur = achado; cur != -1; cur = veio[cur]) {
                saida[pos * 3 + 0] = cur / PB_ESTADOS_POR_FURO;
                saida[pos * 3 + 1] = (cur % PB_ESTADOS_POR_FURO) / 5;
                saida[pos * 3 + 2] = tipo[cur];
                pos--;
            }
            n = tam;
        } else {
            n = -3;   /* buffer pequeno */
        }
    }

    heap_free(&h);
    free(g); free(veio); free(tipo); free(fechado);
    return n;
}

PB_EXPORT int pb_versao(void) { return 1; }
