"""Traduz o roteamento para o vocabulario de quem monta a placa.

O desenho mostra caminhos; a bancada tem tres coisas, e so tres:

* **Ponte de solda** - dois furos VIZINHOS ligados so com estanho. Nao precisa de
  fio: encosta um no outro e pronto. E o passo mais barato que existe.

* **Fio reto** - um pedaco de fio nu, cortado no tamanho e soldado nas duas pontas.
  Reto porque dobrar fio fino em angulo certo, no lugar certo, e briga perdida.

* **Junta** - o furo onde duas ou mais coisas se encontram. Uma quina do desenho
  NAO e um fio dobrado: sao dois fios retos e estanho no furo entre eles. O estanho
  alcanca o furo do meio e os vizinhos ortogonais - no maximo uma cruz. Na pratica
  quase tudo e L.

E por isso que o guia de montagem precisa falar assim: quem le com o ferro na mao
nao tem como executar "dobre aqui".
"""
from __future__ import annotations

PASSO_MM = 2.54

# O estanho alcanca o furo do meio e os quatro vizinhos ortogonais. Mais que isso
# nao e junta, e bolha - e a montagem vira ginastica.
MAX_VIZINHOS_NA_JUNTA = 4

TIPOS_DE_TRILHA = ("trace", "trace_top")
FACES = ((0, "solda"), (1, "componentes"))


def _vao(a, b):
    """Distancia em passos entre dois furos alinhados."""
    return max(abs(b[0] - a[0]), abs(b[1] - a[1]))


def _passo_unitario(de, para):
    """Direcao de `de` para `para`, um furo por vez."""
    dx = (para[0] > de[0]) - (para[0] < de[0])
    dy = (para[1] > de[1]) - (para[1] < de[1])
    return dx, dy


def _formato(direcoes):
    """Que desenho o estanho faz no furo: ponta, linha, L, T ou cruz."""
    n = len(direcoes)
    if n >= 4:
        return "cruz"
    if n == 3:
        return "T"
    if n == 2:
        (ax, ay), (bx, by) = sorted(direcoes)
        # vizinhos em lados opostos: o estanho segue reto
        return "linha" if (ax == -bx and ay == -by) else "L"
    return "ponta"


def plano_de_montagem(rotas, nome_do_furo):
    """Fios, pontes e juntas de todas as redes, no vocabulario da bancada.

    `nome_do_furo(col, row)` da o nome que a placa de quem monta usa - o guia tem
    de citar o mesmo furo que o desenho.
    """
    fios, pontes, juntas, avisos = [], [], [], []
    # furo -> direcoes de tudo que sai dele, por rede e face
    encontros = {}

    for rota in rotas:
        for face, rotulo in FACES:
            for seg in rota["segments"]:
                if seg.get("layer") != face or seg["type"] not in TIPOS_DE_TRILHA:
                    continue
                a, b = tuple(seg["from"]), tuple(seg["to"])
                vao = _vao(a, b)
                item = {
                    "net": rota["name"], "face": rotulo,
                    "de": list(a), "ate": list(b),
                    "de_label": nome_do_furo(*a), "ate_label": nome_do_furo(*b),
                    "furos": vao + 1,
                    "mm": round(vao * PASSO_MM, 1),
                }
                # Dois furos vizinhos nao pedem fio nenhum: e so estanho.
                (pontes if vao == 1 else fios).append(item)

                for ponta, outra in ((a, b), (b, a)):
                    chave = (rota["name"], face, ponta)
                    encontros.setdefault(chave, set()).add(_passo_unitario(ponta, outra))

    for (net, face, furo), direcoes in sorted(encontros.items()):
        if len(direcoes) < 2:
            continue        # ponta de fio nao e junta
        rotulo = dict(FACES)[face]
        vizinhos = [(furo[0] + dx, furo[1] + dy) for dx, dy in sorted(direcoes)]
        item = {
            "net": net, "face": rotulo,
            "furo": list(furo), "furo_label": nome_do_furo(*furo),
            # o estanho toca o furo do meio e estes vizinhos, nunca mais longe
            "toca": [list(v) for v in vizinhos],
            "toca_labels": [nome_do_furo(*v) for v in vizinhos],
            "formato": _formato(direcoes),
        }
        juntas.append(item)
        if len(direcoes) > MAX_VIZINHOS_NA_JUNTA:
            avisos.append(
                "junta em %s da rede %s precisaria alcancar %d vizinhos; o estanho "
                "so faz cruz (4)" % (item["furo_label"], net, len(direcoes)))

    fios.sort(key=lambda f: (-f["furos"], f["net"]))
    pontes.sort(key=lambda p: (p["net"], p["de"]))
    juntas.sort(key=lambda j: (j["net"], j["furo"]))

    return {
        "fios": fios,
        "pontes": pontes,
        "juntas": juntas,
        "avisos": avisos,
        "totais": {
            "fios": len(fios),
            "fio_mm": round(sum(f["mm"] for f in fios), 1),
            "fio_maior_furos": max([f["furos"] for f in fios], default=0),
            "pontes": len(pontes),
            "juntas": len(juntas),
            "linha": sum(1 for j in juntas if j["formato"] == "linha"),
            "L": sum(1 for j in juntas if j["formato"] == "L"),
            "T": sum(1 for j in juntas if j["formato"] == "T"),
            "cruz": sum(1 for j in juntas if j["formato"] == "cruz"),
        },
    }
