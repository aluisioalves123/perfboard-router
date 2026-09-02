"""Compara os nucleos em C com as implementacoes de referencia em Python.

O C existe so para ir mais rapido. Se ele passar a trabalhar PIOR, o programa fica
errado de um jeito traicoeiro: continua entregando layout legal, so que fechando
menos redes - e a busca inteira perde eficacia sem nenhum sintoma visivel.

Ha dois nucleos, e eles precisam de testes DIFERENTES:

* **Roteador (A\\*)** - e exato. Dado o mesmo posicionamento, o C tem que fechar
  tantas redes quanto o Python, caso a caso, sem folga. Por isso os testes de
  roteamento FIXAM as posicoes (`auto_place=False`) e variam so o motor.

  Dois bugs desta familia ja passaram por aqui: o heap desempatava por `f` quando
  o `heapq` do Python desempata por `(f, g, no)`. Em placa folgada nao muda nada;
  em placa congestionada o C fechava menos redes.

* **Posicionador (recozimento)** - e heuristico. Gerador aleatorio diferente ja
  produz outro resultado, e isso e legitimo. Aqui a exigencia e por INVARIANTE
  (nada sobreposto, nada fora da placa, travada imovel) e por qualidade AGREGADA.

Cuidado ao mexer: `PERFBOARD_SEM_C=1` desliga os DOIS nucleos de uma vez. Um teste
que deixe o posicionamento variar entre as duas execucoes nao mede o roteador -
mede sorte. Foi assim que este arquivo comecou a falhar por engano.
"""
import json
import os
import subprocess
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from perfboard import nativo  # noqa: E402

EXEMPLOS = [os.path.join(RAIZ, "examples", n)
            for n in ("astavel_555.net", "registrador_595.net")]

# Casos deliberadamente apertados: placa justa, sem jumper, duas faces. E onde a
# escolha de caminho pesa e onde uma implementacao pior aparece.
CASOS = [
    # (indice do arquivo, colunas, linhas, faces, jumpers, sementes)
    (0, 22, 20, 1, True,  (1, 2, 3)),
    (0, 14, 12, 2, False, (1, 2, 3, 4)),
    (1, 22, 20, 2, False, (1, 2, 3, 4)),
    (1, 20, 18, 1, True,  (1, 2, 3)),
]

# Roteia SEMPRE as mesmas posicoes, geradas em Python nas duas execucoes. Assim a
# unica diferenca entre elas e o motor de ROTEAMENTO.
SCRIPT_ROTEADOR = r'''
import json, os, sys
sys.path.insert(0, %r)
os.environ["PERFBOARD_SEM_C"] = "1"          # posicoes sempre pelo mesmo caminho
import perfboard.nativo as nativo
from perfboard.project import solve
arquivos, casos, com_c = %r, %r, %r

posicoes = {}
for idx, cols, rows, faces, jumpers, sementes in casos:
    with open(arquivos[idx], encoding="utf-8") as fh:
        texto = fh.read()
    for seed in sementes:
        r = solve({"netlist": texto, "board": {"cols": cols, "rows": rows},
                   "placer": {"effort": "rapido", "seed": seed},
                   "router": {"faces": faces, "allow_jumpers": jumpers, "attempts": 6}})
        posicoes[(idx, cols, rows, faces, jumpers, seed)] = r["layout"]["placements"]

if com_c:                                    # so agora liga o motor sob teste
    nativo._DESLIGADO = False
    nativo._LIB = nativo._carrega()

saida = []
for (idx, cols, rows, faces, jumpers, seed), pos in posicoes.items():
    with open(arquivos[idx], encoding="utf-8") as fh:
        texto = fh.read()
    r = solve({"netlist": texto, "board": {"cols": cols, "rows": rows},
               "placements": pos, "auto_place": False,
               "router": {"faces": faces, "allow_jumpers": jumpers, "attempts": 6}})
    s = r["stats"]
    saida.append({
        "arquivo": os.path.basename(arquivos[idx]),
        "placa": "%%dx%%d" %% (cols, rows), "faces": faces,
        "jumpers_ok": jumpers, "seed": seed,
        "soltos": len(s["orphan_pins"]), "roteadas": s["nets_routed"],
        "mm": s["total_mm"], "problemas": len(r["problems"]),
        "motor_c": nativo.disponivel(),
    })
print(json.dumps(saida))
'''


def roda_roteador(com_c: bool):
    codigo = SCRIPT_ROTEADOR % (RAIZ, EXEMPLOS, CASOS, com_c)
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True,
                       text=True, cwd=RAIZ, timeout=900)
    if r.returncode != 0:
        raise AssertionError("execucao falhou (com_c=%s):\n%s" % (com_c, r.stderr[-2000:]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def _rotulo(c):
    return "%s %s faces=%d jumpers=%s seed=%d" % (
        c["arquivo"], c["placa"], c["faces"], c["jumpers_ok"], c["seed"])


@unittest.skipUnless(nativo.disponivel(),
                     "nucleo C nao compilado: %s" % nativo.descricao())
class TestRoteadorC(unittest.TestCase):
    """Mesmas posicoes, dois roteadores. Exato: o C nao pode fechar menos."""

    @classmethod
    def setUpClass(cls):
        cls.com_c = roda_roteador(True)
        cls.sem_c = roda_roteador(False)

    def test_o_motor_certo_foi_usado(self):
        self.assertTrue(all(c["motor_c"] for c in self.com_c), "o C nao foi carregado")
        self.assertFalse(any(c["motor_c"] for c in self.sem_c), "o C vazou para o run Python")
        self.assertGreaterEqual(len(self.com_c), 12, "amostra pequena demais")

    def test_c_nao_fecha_menos_que_o_python(self):
        piores = [
            "%s: C %d soltos vs Python %d" % (_rotulo(c), c["soltos"], py["soltos"])
            for c, py in zip(self.com_c, self.sem_c) if c["soltos"] > py["soltos"]
        ]
        self.assertEqual(piores, [], "C fechou menos redes que o Python em: %s" % piores)

    def test_total_de_pinos_soltos_nao_piora(self):
        soma_c = sum(c["soltos"] for c in self.com_c)
        soma_py = sum(c["soltos"] for c in self.sem_c)
        self.assertLessEqual(soma_c, soma_py,
                             "somando todos os casos, C deixou %d soltos e Python %d"
                             % (soma_c, soma_py))

    def test_amostra_tem_caso_dificil(self):
        """Conjunto so de casos faceis passa mesmo com o nucleo quebrado."""
        self.assertTrue([c for c in self.sem_c if c["soltos"] > 0],
                        "nenhum caso dificil na amostra - este teste nao pegaria "
                        "uma regressao de qualidade de roteamento")

    def test_nunca_gera_layout_ilegal(self):
        for c in self.com_c:
            self.assertEqual(c["problemas"], 0, "%s saiu com problema fisico" % _rotulo(c))


@unittest.skipUnless(nativo.tem_posicionador(), "posicionador em C nao compilado")
class TestPosicionadorC(unittest.TestCase):
    """Heuristico: exige invariantes sempre e qualidade agregada comparavel."""

    # Amostra grande de proposito: com 5 sementes a variancia entre duas heuristicas
    # chega a 23%, e o teste acusava regressao que nao existia. Com 20 a diferenca
    # real aparece - medida em 3% de media, 2% de mediana.
    SEMENTES = tuple(range(1, 21))

    @classmethod
    def setUpClass(cls):
        import random
        import perfboard.placer as PL
        import perfboard.nativo as nat
        from perfboard import footprints as fpmod
        from perfboard.board import BoardSpec
        from perfboard.netlist import parse_netlist
        from perfboard.project import build_layout

        with open(EXEMPLOS[1], encoding="utf-8") as fh:
            nl = parse_netlist(fh.read())
        lib = fpmod.build_library(nl)
        spec = BoardSpec(22, 20)

        def rodada(usar_c):
            antes = nat.tem_posicionador
            nat.tem_posicionador = (lambda: usar_c)
            saida = []
            try:
                for seed in cls.SEMENTES:
                    lay = build_layout(nl, spec, lib, None)
                    PL.initial_pack(lay, nl, random.Random(seed))
                    travadas = sorted(lay.placements)[:2]
                    for ref in travadas:
                        lay.placements[ref].locked = True
                    pos0 = {r: (lay.placements[r].col, lay.placements[r].row,
                                lay.placements[r].rot) for r in travadas}
                    rel = PL.auto_place(lay, nl, seed=seed, effort="normal",
                                        keep_existing=True)
                    st = PL._State(lay, nl, edge_pull=0.6)
                    moveu = [r for r in travadas
                             if (lay.placements[r].col, lay.placements[r].row,
                                 lay.placements[r].rot) != pos0[r]]
                    saida.append({
                        "seed": seed, "custo": st.cost, "sobrep": st.overlap,
                        "fora": st.tot_outside, "travadas_movidas": moveu,
                        "motor": rel.get("motor"),
                        "rotacoes": sorted({p.rot for p in lay.placements.values()}),
                    })
            finally:
                nat.tem_posicionador = antes
            return saida

        cls.com_c = rodada(True)
        cls.sem_c = rodada(False)

    def test_motor_certo(self):
        self.assertTrue(all(r["motor"] == "C" for r in self.com_c))
        self.assertTrue(all(r["motor"] == "Python" for r in self.sem_c))

    def test_nunca_sobrepoe(self):
        for r in self.com_c:
            self.assertEqual(r["sobrep"], 0, "semente %d terminou com %d sobreposicoes"
                             % (r["seed"], r["sobrep"]))

    def test_nada_fora_da_placa(self):
        for r in self.com_c:
            self.assertEqual(r["fora"], 0, "semente %d deixou %d pino(s) fora"
                             % (r["seed"], r["fora"]))

    def test_travadas_nao_se_movem(self):
        for r in self.com_c:
            self.assertEqual(r["travadas_movidas"], [],
                             "semente %d moveu peca travada: %s"
                             % (r["seed"], r["travadas_movidas"]))

    def test_usa_as_quatro_rotacoes(self):
        """Regressao do bug da `gira()`: com `rot & 3` o C so alcancava 0 e 180."""
        vistas = set()
        for r in self.com_c:
            vistas.update(r["rotacoes"])
        self.assertTrue(vistas & {90, 270},
                        "nenhuma peca ficou em 90 ou 270 graus em %d sementes - "
                        "sinal de orientacao colapsada" % len(self.com_c))

    def test_qualidade_agregada_comparavel(self):
        media_c = sum(r["custo"] for r in self.com_c) / len(self.com_c)
        media_py = sum(r["custo"] for r in self.sem_c) / len(self.sem_c)
        # 12% e folga confortavel sobre os 3% medidos, e ainda pega degradacao real
        self.assertLessEqual(media_c, media_py * 1.12,
                             "posicionamento em C ficou %.0f%% pior que o Python "
                             "(%.0f contra %.0f)"
                             % ((media_c / media_py - 1) * 100, media_c, media_py))


@unittest.skipUnless(nativo.disponivel(), "nucleo C nao compilado")
class TestGiraC(unittest.TestCase):
    """A rotacao em C tem que concordar com a do Python, casa a casa.

    Regressao: `gira()` fazia `rot & 3` sobre um valor em GRAUS, entao 90 virava
    180, 180 virava 0 e 270 virava 180 - so duas das quatro orientacoes existiam.
    O posicionador otimizava uma geometria diferente da que devolvia e o custo
    final ficava 3x pior, sem nenhum sintoma alem do resultado ruim.

    Os offsets aqui sao ASSIMETRICOS de proposito: com (1,1) o bug nao aparece,
    porque 90 e 270 dao o mesmo resultado por acidente.
    """

    OFFSETS = [(2, 1), (3, 0), (0, 3), (1, 2), (4, 7), (-2, 5), (5, -3)]

    @classmethod
    def setUpClass(cls):
        import ctypes
        from perfboard import nativo as nat
        if nat._LIB is None or not hasattr(nat._LIB, "pb_gira"):
            raise unittest.SkipTest("biblioteca sem pb_gira")
        cls.lib = nat._LIB
        cls.lib.pb_gira.argtypes = [ctypes.c_int] * 3 + [ctypes.POINTER(ctypes.c_int)] * 2
        cls.ctypes = ctypes

    def test_bate_com_o_python(self):
        from perfboard.board import rotate
        ct = self.ctypes
        for dx, dy in self.OFFSETS:
            for rot in (0, 90, 180, 270):
                ox, oy = ct.c_int(), ct.c_int()
                self.lib.pb_gira(dx, dy, rot, ct.byref(ox), ct.byref(oy))
                self.assertEqual((ox.value, oy.value), rotate(dx, dy, rot),
                                 "rotacao de (%d,%d) por %d graus divergiu" % (dx, dy, rot))

    def test_as_quatro_orientacoes_sao_distintas(self):
        ct = self.ctypes
        for dx, dy in [(2, 1), (4, 7), (5, -3)]:
            vistos = set()
            for rot in (0, 90, 180, 270):
                ox, oy = ct.c_int(), ct.c_int()
                self.lib.pb_gira(dx, dy, rot, ct.byref(ox), ct.byref(oy))
                vistos.add((ox.value, oy.value))
            self.assertEqual(len(vistos), 4,
                             "offset (%d,%d): as 4 rotacoes deveriam dar 4 resultados "
                             "diferentes, deram %d" % (dx, dy, len(vistos)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
