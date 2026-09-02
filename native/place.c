/* Nucleo em C do posicionador: o recozimento simulado.
 *
 * Por que existe: medido por ablacao, a manutencao de corpo/ocupacao responde por
 * ~91% do tempo de uma tentativa, e nao ha o que melhorar algoritmicamente ali - sao
 * ~100 mil movimentos, cada um mexendo em algumas dezenas de celulas. E volume bruto
 * de iteracao num laco numerico simples, exatamente onde Python perde feio.
 *
 * Diferenca importante para o A* (perfboard.c): aquele e exato, entao exigimos
 * caminho identico ao Python. Este e HEURISTICO - gerador de numeros diferente ja
 * produz outro resultado, e isso e legitimo. A validacao e por invariante (nenhuma
 * sobreposicao, nada fora da placa, peca travada nao se move) e por qualidade
 * agregada em varias sementes.
 *
 * Compila junto com perfboard.c na mesma biblioteca.
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifdef _WIN32
#define PB_EXPORT __declspec(dllexport)
#else
#define PB_EXPORT
#endif

#define PB_ROTS 4

typedef struct {
    double w_overlap_final, w_overlap_inicial;
    double w_outside, w_corpo_fora, w_desacopla;
    double edge_pull;
    double sem_saida_0, sem_saida_1, sem_saida_2;   /* penalidade por 0,1,2 saidas */
    int folga_desacopla;
    int proibir_sobreposicao;   /* 1 = movimento que sobrepoe e recusado na hora */
    int passos;
    unsigned long long semente;
} PbPlaceCfg;

/* ---------------------------------------------------------------- estado */

typedef struct {
    int cols, rows, x0, y0, x1, y1;

    int n_comp;
    const int *pin_ini, *pin_qtd;      /* faixa de pinos de cada peca */
    const int *pin_dx, *pin_dy;        /* offsets, na orientacao original */
    const int *pin_net;                /* rede de cada pino (-1 = sem rede) */
    const int *corpo;                  /* 4 por peca: bx0,by0,bx1,by1 */
    const unsigned char *movel, *borda;

    int *col, *row, *rot;              /* estado corrente (entra e sai) */

    int n_net;
    const int *net_ini, *net_qtd;
    const int *net_pino;               /* indice global de pino */

    int n_par;
    const int *par_a, *par_b;          /* indices globais de pino */

    /* derivados */
    int *pc_x, *pc_y;                  /* celula de cada pino */
    int *occ;                          /* contagem por celula */
    int *rede_da_celula;               /* rede do pino que ocupa a celula, -1 */
    int *dono_da_celula;               /* peca dona, para desfazer */
    int *cx0, *cy0, *cx1, *cy1;        /* retangulo do corpo, ja girado */
    double *custo_net, *custo_par, *pen;
    int *fora_pino, *fora_corpo;
    double *custo_borda;

    double fio, laco, trancado, w_overlap;
    int overlap, tot_fora_pino, tot_fora_corpo;
    double tot_borda;

    const PbPlaceCfg *cfg;
} PbEstado;

static inline int idx(const PbEstado *e, int c, int r) { return r * e->cols + c; }
static inline int dentro(const PbEstado *e, int c, int r) {
    return c >= e->x0 && c <= e->x1 && r >= e->y0 && r <= e->y1;
}

static void gira(int dx, int dy, int rot, int *ox, int *oy) {
    /* `rot` chega em GRAUS (0/90/180/270), nao em quartos de volta.
     *
     * Aqui morava um bug caro: `rot & 3` fazia 90 virar 180, 180 virar 0 e 270
     * virar 180 - so duas das quatro orientacoes existiam, e trocadas. O C
     * otimizava uma geometria diferente da que devolvia, e o custo final ficava em
     * 8836 contra 2818 do Python. Corrigido, empata com a referencia.
     *
     * Ha teste de regressao comparando esta funcao com board.rotate() usando
     * offsets ASSIMETRICOS: com (1,1) o bug nao aparece. */
    int q = ((rot / 90) % 4 + 4) % 4;
    switch (q) {
        case 0: *ox = dx;  *oy = dy;  break;
        case 1: *ox = -dy; *oy = dx;  break;   /* 90 */
        case 2: *ox = -dx; *oy = -dy; break;   /* 180 */
        default: *ox = dy; *oy = -dx; break;   /* 270 */
    }
}

/* Extremos do corpo depois de girado, para nao sortear posicao que joga a peca
 * para fora da placa. O Python faz isso em `_random_position`; sem o equivalente
 * aqui, metade das realocacoes nascia invalida. */
static void extremos_corpo(const int *b, int rot, int *mnx, int *mny, int *mxx, int *mxy) {
    int xs[4], ys[4];
    gira(b[0], b[1], rot, &xs[0], &ys[0]);
    gira(b[2], b[1], rot, &xs[1], &ys[1]);
    gira(b[0], b[3], rot, &xs[2], &ys[2]);
    gira(b[2], b[3], rot, &xs[3], &ys[3]);
    *mnx = *mxx = xs[0]; *mny = *mxy = ys[0];
    for (int k = 1; k < 4; k++) {
        if (xs[k] < *mnx) *mnx = xs[k];
        if (xs[k] > *mxx) *mxx = xs[k];
        if (ys[k] < *mny) *mny = ys[k];
        if (ys[k] > *mxy) *mxy = ys[k];
    }
}

/* xorshift: barato e reprodutivel a partir da semente */
static inline unsigned long long prox(unsigned long long *s) {
    unsigned long long x = *s;
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    return (*s = x);
}
static inline double aleatorio(unsigned long long *s) {
    return (double)(prox(s) >> 11) / 9007199254740992.0;
}
static inline int inteiro(unsigned long long *s, int lo, int hi) {
    if (hi <= lo) return lo;
    return lo + (int)(prox(s) % (unsigned long long)(hi - lo + 1));
}

/* ---------------------------------------------------------------- penalidade */

static double pen_celula(const PbEstado *e, int c, int r) {
    if (c < 0 || c >= e->cols || r < 0 || r >= e->rows) return 0.0;
    int i = idx(e, c, r);
    int rede = e->rede_da_celula[i];
    if (rede < 0) return 0.0;
    int livres = 0;
    static const int DC[4] = {1, -1, 0, 0}, DR[4] = {0, 0, 1, -1};
    for (int k = 0; k < 4; k++) {
        int vc = c + DC[k], vr = r + DR[k];
        if (!dentro(e, vc, vr)) continue;
        int outro = e->rede_da_celula[idx(e, vc, vr)];
        if (outro < 0 || outro == rede) livres++;
    }
    if (livres == 0) return e->cfg->sem_saida_0;
    if (livres == 1) return e->cfg->sem_saida_1;
    if (livres == 2) return e->cfg->sem_saida_2;
    return 0.0;
}

static double repen(PbEstado *e, const int *celulas, int n) {
    double delta = 0.0;
    for (int k = 0; k < n; k++) {
        int i = celulas[k];
        if (i < 0) continue;
        double antes = e->pen[i];
        double agora = pen_celula(e, i % e->cols, i / e->cols);
        e->pen[i] = agora;
        delta += agora - antes;
    }
    return delta;
}

/* ---------------------------------------------------------------- instala */

static void desinstala(PbEstado *e, int p) {
    for (int k = e->pin_ini[p]; k < e->pin_ini[p] + e->pin_qtd[p]; k++) {
        int c = e->pc_x[k], r = e->pc_y[k];
        if (c >= 0 && c < e->cols && r >= 0 && r < e->rows) {
            int i = idx(e, c, r);
            if (e->dono_da_celula[i] == p) {
                e->dono_da_celula[i] = -1;
                e->rede_da_celula[i] = -1;
            }
        }
    }
    for (int r = e->cy0[p]; r <= e->cy1[p]; r++)
        for (int c = e->cx0[p]; c <= e->cx1[p]; c++) {
            if (c < 0 || c >= e->cols || r < 0 || r >= e->rows) continue;
            int i = idx(e, c, r);
            if (e->occ[i] >= 2) e->overlap--;
            e->occ[i]--;
        }
}

static void instala(PbEstado *e, int p) {
    int cx0 = 1 << 28, cy0 = 1 << 28, cx1 = -(1 << 28), cy1 = -(1 << 28);
    int fora_p = 0;

    for (int k = e->pin_ini[p]; k < e->pin_ini[p] + e->pin_qtd[p]; k++) {
        int ox, oy;
        gira(e->pin_dx[k], e->pin_dy[k], e->rot[p], &ox, &oy);
        int c = e->col[p] + ox, r = e->row[p] + oy;
        e->pc_x[k] = c; e->pc_y[k] = r;
        if (!dentro(e, c, r)) fora_p++;
        if (c >= 0 && c < e->cols && r >= 0 && r < e->rows) {
            int i = idx(e, c, r);
            if (e->pin_net[k] >= 0 && e->dono_da_celula[i] < 0) {
                e->dono_da_celula[i] = p;
                e->rede_da_celula[i] = e->pin_net[k];
            }
        }
    }

    /* retangulo do corpo, girado */
    const int *b = e->corpo + p * 4;
    int xs[4], ys[4];
    gira(b[0], b[1], e->rot[p], &xs[0], &ys[0]);
    gira(b[2], b[1], e->rot[p], &xs[1], &ys[1]);
    gira(b[0], b[3], e->rot[p], &xs[2], &ys[2]);
    gira(b[2], b[3], e->rot[p], &xs[3], &ys[3]);
    for (int k = 0; k < 4; k++) {
        int c = e->col[p] + xs[k], r = e->row[p] + ys[k];
        if (c < cx0) cx0 = c;
        if (c > cx1) cx1 = c;
        if (r < cy0) cy0 = r;
        if (r > cy1) cy1 = r;
    }
    e->cx0[p] = cx0; e->cy0[p] = cy0; e->cx1[p] = cx1; e->cy1[p] = cy1;

    int fora_c = 0;
    for (int r = cy0; r <= cy1; r++)
        for (int c = cx0; c <= cx1; c++) {
            if (!dentro(e, c, r)) fora_c++;
            if (c < 0 || c >= e->cols || r < 0 || r >= e->rows) continue;
            int i = idx(e, c, r);
            if (e->occ[i] >= 1) e->overlap++;
            e->occ[i]++;
        }

    e->tot_fora_pino += fora_p - e->fora_pino[p];
    e->fora_pino[p] = fora_p;
    e->tot_fora_corpo += fora_c - e->fora_corpo[p];
    e->fora_corpo[p] = fora_c;

    double nb = 0.0;
    if (e->cfg->edge_pull > 0.0 && e->borda[p]) {
        double ccx = (cx0 + cx1) / 2.0, ccy = (cy0 + cy1) / 2.0;
        double m = ccx - e->x0;
        if (e->x1 - ccx < m) m = e->x1 - ccx;
        if (ccy - e->y0 < m) m = ccy - e->y0;
        if (e->y1 - ccy < m) m = e->y1 - ccy;
        nb = m * e->cfg->edge_pull;
    }
    e->tot_borda += nb - e->custo_borda[p];
    e->custo_borda[p] = nb;
}

/* ---------------------------------------------------------------- custos */

static double calc_net(const PbEstado *e, int n) {
    int minx = 1 << 28, maxx = -(1 << 28), miny = 1 << 28, maxy = -(1 << 28), qtd = 0;
    for (int k = e->net_ini[n]; k < e->net_ini[n] + e->net_qtd[n]; k++) {
        int g = e->net_pino[k];
        int c = e->pc_x[g], r = e->pc_y[g];
        if (c < minx) minx = c;
        if (c > maxx) maxx = c;
        if (r < miny) miny = r;
        if (r > maxy) maxy = r;
        qtd++;
    }
    if (qtd < 2) return 0.0;
    return (double)((maxx - minx) + (maxy - miny));
}

static double calc_par(const PbEstado *e, int i) {
    int a = e->par_a[i], b = e->par_b[i];
    int d = abs(e->pc_x[a] - e->pc_x[b]) + abs(e->pc_y[a] - e->pc_y[b]);
    d -= e->cfg->folga_desacopla;
    return d > 0 ? e->cfg->w_desacopla * d : 0.0;
}

static double custo(const PbEstado *e) {
    return e->fio
         + e->w_overlap * (double)e->overlap
         + e->cfg->w_outside * (double)e->tot_fora_pino
         + e->cfg->w_corpo_fora * (double)e->tot_fora_corpo
         + e->trancado
         + e->laco
         + e->tot_borda;
}

/* ---------------------------------------------------------------- principal */

PB_EXPORT int pb_place(const PbPlaceCfg *cfg,
                       int cols, int rows, int margem,
                       int n_comp, const int *pin_ini, const int *pin_qtd,
                       const int *pin_dx, const int *pin_dy, const int *pin_net,
                       const int *corpo, const unsigned char *movel,
                       const unsigned char *borda,
                       int n_net, const int *net_ini, const int *net_qtd,
                       const int *net_pino,
                       int n_par, const int *par_a, const int *par_b,
                       int n_pinos_total,
                       int *col, int *row, int *rot)
{
    PbEstado e;
    memset(&e, 0, sizeof(e));
    e.cols = cols; e.rows = rows;
    e.x0 = margem; e.y0 = margem; e.x1 = cols - 1 - margem; e.y1 = rows - 1 - margem;
    e.n_comp = n_comp;
    e.pin_ini = pin_ini; e.pin_qtd = pin_qtd;
    e.pin_dx = pin_dx; e.pin_dy = pin_dy; e.pin_net = pin_net;
    e.corpo = corpo; e.movel = movel; e.borda = borda;
    e.col = col; e.row = row; e.rot = rot;
    e.n_net = n_net; e.net_ini = net_ini; e.net_qtd = net_qtd; e.net_pino = net_pino;
    e.n_par = n_par; e.par_a = par_a; e.par_b = par_b;
    e.cfg = cfg;

    const int N = cols * rows;
    e.pc_x = (int *)malloc(sizeof(int) * n_pinos_total);
    e.pc_y = (int *)malloc(sizeof(int) * n_pinos_total);
    e.occ = (int *)calloc(N, sizeof(int));
    e.rede_da_celula = (int *)malloc(sizeof(int) * N);
    e.dono_da_celula = (int *)malloc(sizeof(int) * N);
    e.pen = (double *)calloc(N, sizeof(double));
    e.cx0 = (int *)calloc(n_comp, sizeof(int));
    e.cy0 = (int *)calloc(n_comp, sizeof(int));
    e.cx1 = (int *)calloc(n_comp, sizeof(int));
    e.cy1 = (int *)calloc(n_comp, sizeof(int));
    e.custo_net = (double *)calloc(n_net > 0 ? n_net : 1, sizeof(double));
    e.custo_par = (double *)calloc(n_par > 0 ? n_par : 1, sizeof(double));
    e.fora_pino = (int *)calloc(n_comp, sizeof(int));
    e.fora_corpo = (int *)calloc(n_comp, sizeof(int));
    e.custo_borda = (double *)calloc(n_comp, sizeof(double));
    int *moveis = (int *)malloc(sizeof(int) * (n_comp > 0 ? n_comp : 1));
    int *melhor_col = (int *)malloc(sizeof(int) * n_comp);
    int *melhor_row = (int *)malloc(sizeof(int) * n_comp);
    int *melhor_rot = (int *)malloc(sizeof(int) * n_comp);
    int *afetadas = (int *)malloc(sizeof(int) * 130 * 5);

    if (!e.pc_x || !e.pc_y || !e.occ || !e.rede_da_celula || !e.dono_da_celula ||
        !e.pen || !e.cx0 || !e.custo_net || !moveis || !melhor_col || !afetadas) {
        free(e.pc_x); free(e.pc_y); free(e.occ); free(e.rede_da_celula);
        free(e.dono_da_celula); free(e.pen); free(e.cx0); free(e.cy0);
        free(e.cx1); free(e.cy1); free(e.custo_net); free(e.custo_par);
        free(e.fora_pino); free(e.fora_corpo); free(e.custo_borda);
        free(moveis); free(melhor_col); free(melhor_row); free(melhor_rot);
        free(afetadas);
        return -2;
    }

    for (int i = 0; i < N; i++) { e.rede_da_celula[i] = -1; e.dono_da_celula[i] = -1; }
    for (int i = 0; i < n_pinos_total; i++) { e.pc_x[i] = -9999; e.pc_y[i] = -9999; }

    int n_moveis = 0;
    for (int p = 0; p < n_comp; p++) if (movel[p]) moveis[n_moveis++] = p;

    e.w_overlap = cfg->w_overlap_inicial;
    for (int p = 0; p < n_comp; p++) instala(&e, p);
    for (int n = 0; n < n_net; n++) { e.custo_net[n] = calc_net(&e, n); e.fio += e.custo_net[n]; }
    for (int i = 0; i < n_par; i++) { e.custo_par[i] = calc_par(&e, i); e.laco += e.custo_par[i]; }
    for (int i = 0; i < N; i++) { e.pen[i] = pen_celula(&e, i % cols, i / cols); e.trancado += e.pen[i]; }

    if (n_moveis == 0) {
        free(e.pc_x); free(e.pc_y); free(e.occ); free(e.rede_da_celula);
        free(e.dono_da_celula); free(e.pen); free(e.cx0); free(e.cy0);
        free(e.cx1); free(e.cy1); free(e.custo_net); free(e.custo_par);
        free(e.fora_pino); free(e.fora_corpo); free(e.custo_borda);
        free(moveis); free(melhor_col); free(melhor_row); free(melhor_rot);
        free(afetadas);
        return 0;
    }

    unsigned long long s = cfg->semente ? cfg->semente : 88172645463325252ULL;
    double c_atual = custo(&e);
    double t0 = c_atual / (double)n_moveis * 0.6;
    if (t0 < 4.0) t0 = 4.0;
    const double t_fim = 0.05;

    double estrito = c_atual + (cfg->w_overlap_final - e.w_overlap) * e.overlap;
    double melhor = estrito;
    memcpy(melhor_col, col, sizeof(int) * n_comp);
    memcpy(melhor_row, row, sizeof(int) * n_comp);
    memcpy(melhor_rot, rot, sizeof(int) * n_comp);

    for (int passo = 0; passo < cfg->passos; passo++) {
        double frac = (double)passo / (double)cfg->passos;
        double t = t0 * pow(t_fim / t0, frac);

        double novo_w = cfg->w_overlap_inicial
            + (cfg->w_overlap_final - cfg->w_overlap_inicial) * (frac / 0.8 < 1.0 ? frac / 0.8 : 1.0);
        if (e.overlap && novo_w != e.w_overlap) c_atual += (novo_w - e.w_overlap) * e.overlap;
        e.w_overlap = novo_w;

        int p = moveis[inteiro(&s, 0, n_moveis - 1)];
        int old_c = col[p], old_r = row[p], old_rot = rot[p];
        int overlap_antes = e.overlap;

        /* celulas afetadas pela penalidade: pinos antigos e novos, mais vizinhos */
        int n_af = 0;
        for (int k = pin_ini[p]; k < pin_ini[p] + pin_qtd[p] && n_af < 60 * 5; k++) {
            int c = e.pc_x[k], r = e.pc_y[k];
            static const int DC[5] = {0, 1, -1, 0, 0}, DR[5] = {0, 0, 0, 1, -1};
            for (int j = 0; j < 5; j++) {
                int vc = c + DC[j], vr = r + DR[j];
                afetadas[n_af++] = (vc >= 0 && vc < cols && vr >= 0 && vr < rows)
                                 ? idx(&e, vc, vr) : -1;
            }
        }

        double sorte = aleatorio(&s);
        int q = -1, old_c2 = 0, old_r2 = 0, old_rot2 = 0;

        if (sorte < 0.62) {                       /* deslocamento local */
            int d = (t < t0 * 0.2) ? 1 : 3;
            col[p] += inteiro(&s, -d, d);
            row[p] += inteiro(&s, -d, d);
        } else if (sorte < 0.78) {                /* rotacao */
            rot[p] = inteiro(&s, 0, 3) * 90;
        } else if (sorte < 0.90) {                /* realocacao, dentro da placa */
            int nr = inteiro(&s, 0, 3) * 90;
            int mnx, mny, mxx, mxy;
            extremos_corpo(corpo + p * 4, nr, &mnx, &mny, &mxx, &mxy);
            int lo_c = e.x0 - mnx, hi_c = e.x1 - mxx;
            int lo_r = e.y0 - mny, hi_r = e.y1 - mxy;
            if (hi_c < lo_c) hi_c = lo_c;
            if (hi_r < lo_r) hi_r = lo_r;
            col[p] = inteiro(&s, lo_c, hi_c);
            row[p] = inteiro(&s, lo_r, hi_r);
            rot[p] = nr;
        } else {                                  /* troca de lugar com outra peca */
            q = moveis[inteiro(&s, 0, n_moveis - 1)];
            if (q != p) {
                old_c2 = col[q]; old_r2 = row[q]; old_rot2 = rot[q];
                int pc = col[p], pr = row[p];
                desinstala(&e, q);
                col[q] = pc; row[q] = pr;
                instala(&e, q);
                col[p] = old_c2; row[p] = old_r2;
            } else {
                q = -1;
            }
        }
        rot[p] = ((rot[p] / 90) & 3) * 90;

        desinstala(&e, p);
        instala(&e, p);

        for (int k = pin_ini[p]; k < pin_ini[p] + pin_qtd[p] && n_af < 120 * 5; k++) {
            int c = e.pc_x[k], r = e.pc_y[k];
            static const int DC[5] = {0, 1, -1, 0, 0}, DR[5] = {0, 0, 0, 1, -1};
            for (int j = 0; j < 5; j++) {
                int vc = c + DC[j], vr = r + DR[j];
                afetadas[n_af++] = (vc >= 0 && vc < cols && vr >= 0 && vr < rows)
                                 ? idx(&e, vc, vr) : -1;
            }
        }
        if (q >= 0) {
            for (int k = pin_ini[q]; k < pin_ini[q] + pin_qtd[q] && n_af < 120 * 5; k++) {
                int c = e.pc_x[k], r = e.pc_y[k];
                static const int DC[5] = {0, 1, -1, 0, 0}, DR[5] = {0, 0, 0, 1, -1};
                for (int j = 0; j < 5; j++) {
                    int vc = c + DC[j], vr = r + DR[j];
                    afetadas[n_af++] = (vc >= 0 && vc < cols && vr >= 0 && vr < rows)
                                     ? idx(&e, vc, vr) : -1;
                }
            }
        }
        e.trancado += repen(&e, afetadas, n_af);

        for (int n = 0; n < n_net; n++) {
            double nv = calc_net(&e, n);
            if (nv != e.custo_net[n]) { e.fio += nv - e.custo_net[n]; e.custo_net[n] = nv; }
        }
        for (int i = 0; i < n_par; i++) {
            double nv = calc_par(&e, i);
            if (nv != e.custo_par[i]) { e.laco += nv - e.custo_par[i]; e.custo_par[i] = nv; }
        }

        double c_novo = custo(&e);
        double delta = c_novo - c_atual;
        int aceita;
        if (cfg->proibir_sobreposicao && e.overlap > overlap_antes) {
            /* Duas pecas no mesmo espaco nao existe na bancada. "Proibido" aqui e
             * NUNCA PIORAR: recusamos qualquer movimento que aumente a sobreposicao,
             * entao o numero so cai ate zero e nunca mais sobe.
             *
             * Recusar "qualquer sobreposicao > 0" seria errado: o empacotamento
             * inicial ja nasce com pecas empilhadas, e com aquela regra nenhum
             * movimento seria aceito - o posicionador congelava no lugar. */
            aceita = 0;
        } else {
            aceita = (delta <= 0.0);
            if (!aceita) {
                double x = delta / (t > 1e-6 ? t : 1e-6);
                if (x < 60.0 && aleatorio(&s) < exp(-x)) aceita = 1;
            }
        }

        if (aceita) {
            c_atual = c_novo;
            double est = c_novo + (cfg->w_overlap_final - e.w_overlap) * e.overlap;
            if (est < melhor - 1e-9) {
                melhor = est;
                memcpy(melhor_col, col, sizeof(int) * n_comp);
                memcpy(melhor_row, row, sizeof(int) * n_comp);
                memcpy(melhor_rot, rot, sizeof(int) * n_comp);
            }
        } else {
            desinstala(&e, p);
            col[p] = old_c; row[p] = old_r; rot[p] = old_rot;
            instala(&e, p);
            if (q >= 0) {
                desinstala(&e, q);
                col[q] = old_c2; row[q] = old_r2; rot[q] = old_rot2;
                instala(&e, q);
            }
            e.trancado += repen(&e, afetadas, n_af);
            for (int n = 0; n < n_net; n++) {
                double nv = calc_net(&e, n);
                if (nv != e.custo_net[n]) { e.fio += nv - e.custo_net[n]; e.custo_net[n] = nv; }
            }
            for (int i = 0; i < n_par; i++) {
                double nv = calc_par(&e, i);
                if (nv != e.custo_par[i]) { e.laco += nv - e.custo_par[i]; e.custo_par[i] = nv; }
            }
        }
    }

    memcpy(col, melhor_col, sizeof(int) * n_comp);
    memcpy(row, melhor_row, sizeof(int) * n_comp);
    memcpy(rot, melhor_rot, sizeof(int) * n_comp);

    free(e.pc_x); free(e.pc_y); free(e.occ); free(e.rede_da_celula);
    free(e.dono_da_celula); free(e.pen); free(e.cx0); free(e.cy0);
    free(e.cx1); free(e.cy1); free(e.custo_net); free(e.custo_par);
    free(e.fora_pino); free(e.fora_corpo); free(e.custo_borda);
    free(moveis); free(melhor_col); free(melhor_row); free(melhor_rot);
    free(afetadas);
    return 1;
}

PB_EXPORT int pb_place_versao(void) { return 1; }

/* Exposto so para o teste de regressao comparar com board.rotate() do Python. */
PB_EXPORT void pb_gira(int dx, int dy, int rot, int *ox, int *oy) {
    gira(dx, dy, rot, ox, oy);
}
