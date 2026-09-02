"""Verifica que o resultado do roteador e fisicamente construivel.

Regras checadas:
  * cada ilha (furo + face) pertence a no maximo uma rede;
  * cada aresta de trilha, em cada face, pertence a no maximo uma rede;
  * numa placa de face unica nao existe trilha do lado dos componentes nem via;
  * TODOS os pinos de uma rede marcada `ok` estao no mesmo grafo;
  * pino que sobra e denunciado no relatorio e marcado no desenho.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from perfboard.board import BoardSpec  # noqa: E402
from perfboard.footprints import build_library  # noqa: E402
from perfboard.netlist import parse_netlist  # noqa: E402
from perfboard.project import analyze, build_layout, solve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "..", "examples", "astavel_555.net")

BOTTOM, TOP = 0, 1


def edge_key(a, b):
    a, b = tuple(a), tuple(b)
    return (a, b) if a <= b else (b, a)


def expand_trace(a, b):
    """Furos percorridos por uma trilha reta entre dois furos colineares."""
    (ax, ay), (bx, by) = tuple(a), tuple(b)
    dx = (bx > ax) - (bx < ax)
    dy = (by > ay) - (by < ay)
    assert dx == 0 or dy == 0, "trilha diagonal nao existe em perfboard: %s -> %s" % (a, b)
    holes = [(ax, ay)]
    x, y = ax, ay
    while (x, y) != (bx, by):
        x, y = x + dx, y + dy
        holes.append((x, y))
    return holes


MINI_NET = """(export (version "E")
  (components
    (comp (ref "R1") (value "1k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"))
    (comp (ref "R2") (value "1k")
      (footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal")))
  (nets
    (net (code "1") (name "N1")
      (node (ref "R1") (pin "2"))
      (node (ref "R2") (pin "1")))))
"""


class TestIntegridade(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(EXAMPLE, encoding="utf-8") as fh:
            cls.text = fh.read()
        cls.analysis = analyze(cls.text)

    def solve_case(self, **kw):
        payload = {"netlist": self.text,
                   "board": self.analysis["suggested_board"],
                   "placer": {"effort": "rapido", "seed": 3}}
        payload.update(kw)
        return solve(payload)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _faces(res):
        return res["stats"].get("faces", 1)

    def _pad_key(self, res, cell, face):
        """Numa placa de face unica as duas ilhas do furo sao o mesmo ponto."""
        if self._faces(res) < 2:
            return (cell[0], cell[1], BOTTOM)
        return (cell[0], cell[1], face)

    @staticmethod
    def _face_of(seg):
        return seg.get("layer", BOTTOM)

    def assert_legal(self, res):
        """Nenhuma ilha e nenhuma aresta disputada por duas redes."""
        pad_owner, edge_owner = {}, {}
        faces = self._faces(res)

        for route in res["routes"]:
            if not route["ok"]:
                continue
            net = route["name"]
            for seg in route["segments"]:
                a, b = tuple(seg["from"]), tuple(seg["to"])
                tipo, face = seg["type"], self._face_of(seg)

                if tipo == "trace_top":
                    self.assertGreaterEqual(
                        faces, 2, "trilha do lado dos componentes exige placa de duas faces")
                if tipo == "via":
                    self.assertGreaterEqual(faces, 2, "via exige placa de duas faces")
                    for f in (BOTTOM, TOP):
                        k = self._pad_key(res, a, f)
                        prev = pad_owner.setdefault(k, net)
                        self.assertEqual(prev, net,
                                         "ilha %s disputada por %s e %s" % (k, prev, net))
                    continue

                if tipo in ("trace", "trace_top"):
                    holes = expand_trace(a, b)
                    for i in range(len(holes) - 1):
                        k = (edge_key(holes[i], holes[i + 1]), face)
                        prev = edge_owner.setdefault(k, net)
                        self.assertEqual(prev, net,
                                         "aresta %s disputada por %s e %s" % (k, prev, net))
                else:
                    holes = [a, b]

                for h in holes:
                    k = self._pad_key(res, h, face)
                    prev = pad_owner.setdefault(k, net)
                    self.assertEqual(prev, net,
                                     "ilha %s disputada por %s e %s" % (k, prev, net))

        # o terminal atravessa o furo, entao reserva as DUAS faces para a rede dele
        shorted = {tuple(s["cell"]) for s in res["shorts"]}
        for pin in res["layout"]["pins"]:
            cell = (pin["col"], pin["row"])
            if cell in shorted or not pin["net"]:
                continue
            for f in (BOTTOM, TOP):
                k = self._pad_key(res, cell, f)
                if k in pad_owner:
                    self.assertEqual(pad_owner[k], pin["net"],
                                     "ilha do pino %s.%s tomada pela rede %s"
                                     % (pin["ref"], pin["pin"], pad_owner[k]))

    def _grafo(self, res, route):
        """Reconstroi as ligacoes que serao realmente soldadas."""
        adj = {}

        def liga(a, b):
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        for seg in route["segments"]:
            a, b = tuple(seg["from"]), tuple(seg["to"])
            face = self._face_of(seg)
            if seg["type"] == "via":
                liga((a[0], a[1], BOTTOM), (a[0], a[1], TOP))
            elif seg["type"] in ("trace", "trace_top"):
                holes = expand_trace(a, b)
                for i in range(len(holes) - 1):
                    liga(holes[i] + (face,), holes[i + 1] + (face,))
            else:
                liga(a + (face,), b + (face,))
        return adj

    def assert_pinos_ligados(self, res):
        """Toda rede marcada ok precisa ter TODOS os seus pinos no mesmo grafo.

        Esta e a checagem central: conferir os furos que o proprio roteador diz ter
        usado nao basta - deixava passar batido o pino que ficava de fora.
        """
        pins_por_rede = {}
        for pin in res["layout"]["pins"]:
            if pin["net"]:
                pins_por_rede.setdefault(pin["net"], set()).add((pin["col"], pin["row"]))

        for route in res["routes"]:
            if not route["ok"]:
                continue
            pins = pins_por_rede.get(route["name"], set())
            if len(pins) < 2:
                continue
            adj = self._grafo(res, route)
            # o terminal do componente liga as duas faces do furo
            for cell in pins:
                adj.setdefault(cell + (BOTTOM,), set()).add(cell + (TOP,))
                adj.setdefault(cell + (TOP,), set()).add(cell + (BOTTOM,))

            inicio = next(iter(pins)) + (BOTTOM,)
            seen, stack = {inicio}, [inicio]
            while stack:
                cur = stack.pop()
                for nxt in adj.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)

            soltos = sorted(c for c in pins
                            if c + (BOTTOM,) not in seen and c + (TOP,) not in seen)
            self.assertEqual(soltos, [],
                             "rede %s marcada como ok mas com pino(s) sem ligacao: %s"
                             % (route["name"], soltos))

    def assert_sem_pedacos_soltos(self, res):
        """A fiacao de uma rede nao pode ficar em ilhas separadas."""
        for route in res["routes"]:
            if not route["ok"]:
                continue
            adj = self._grafo(res, route)
            if not adj:
                continue
            pins = {(p["col"], p["row"]) for p in res["layout"]["pins"]
                    if p["net"] == route["name"]}
            for cell in pins:
                adj.setdefault(cell + (BOTTOM,), set()).add(cell + (TOP,))
                adj.setdefault(cell + (TOP,), set()).add(cell + (BOTTOM,))
            inicio = next(iter(adj))
            seen, stack = {inicio}, [inicio]
            while stack:
                cur = stack.pop()
                for nxt in adj.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            self.assertEqual(set(adj) - seen, set(),
                             "rede %s ficou em pedacos separados" % route["name"])

    def assert_tudo(self, res):
        self.assert_legal(res)
        self.assert_pinos_ligados(res)
        self.assert_sem_pedacos_soltos(res)

    # ------------------------------------------------------------------
    # testes
    # ------------------------------------------------------------------

    def test_parser(self):
        s = self.analysis["summary"]
        self.assertEqual(s["components"], 8)
        self.assertEqual(s["nets"], 7)
        self.assertEqual(s["pins"], 22)
        u1 = next(c for c in self.analysis["components"] if c["ref"] == "U1")
        self.assertEqual(u1["pattern"]["pins"]["1"], [0, 0])
        self.assertEqual(u1["pattern"]["pins"]["8"], [3, 0])

    def test_roteamento_completo(self):
        res = self.solve_case()
        self.assertEqual(res["stats"]["nets_failed"], 0, res["stats"]["failed"])
        self.assertEqual(res["shorts"], [])
        self.assertEqual(res["problems"], [])
        self.assert_tudo(res)

    def test_face_unica_nao_usa_via_nem_trilha_de_cima(self):
        res = self.solve_case(router={"faces": 1})
        self.assertEqual(res["stats"]["vias"], 0)
        for route in res["routes"]:
            for seg in route["segments"]:
                self.assertIn(seg["type"], ("trace", "jumper"))
        self.assert_tudo(res)

    def test_duas_faces_dispensa_jumper(self):
        """Com duas faces isoladas, o 555 fecha inteiro sem nenhum jumper."""
        res = self.solve_case(router={"faces": 2, "allow_jumpers": False},
                              placer={"effort": "normal", "seed": 3})
        self.assert_tudo(res)
        self.assertEqual(res["stats"]["jumpers"], 0)
        self.assertEqual(res["stats"]["orphan_pins"], [],
                         "duas faces deveriam fechar este circuito sem jumper")

    def test_sem_jumpers(self):
        """Sem jumper o roteador pode falhar, mas nunca pode gerar layout ilegal."""
        res = self.solve_case(router={"allow_jumpers": False},
                              placer={"effort": "rapido", "seed": 5})
        self.assert_tudo(res)
        for route in res["routes"]:
            for seg in route["segments"]:
                self.assertNotEqual(seg["type"], "jumper")

    def test_pino_solto_nunca_passa_calado(self):
        """Varias sementes, 1 e 2 faces, com e sem jumper: nada de pino solto em silencio."""
        for faces in (1, 2):
            for allow in (True, False):
                for seed in (1, 2, 3):
                    res = self.solve_case(
                        router={"faces": faces, "allow_jumpers": allow},
                        placer={"effort": "rapido", "seed": seed},
                    )
                    self.assert_tudo(res)
                    soltos = res["stats"]["orphan_pins"]
                    if soltos:
                        self.assertFalse(res["ok"])
                        self.assertTrue(res["stats"]["failed"])
                        for o in soltos:
                            self.assertIn("label", o)
                        self.assertIn("SEM LIGACAO", res["svg_top"],
                                      "o desenho precisa marcar o pino solto")

    def test_posicoes_travadas_nao_mudam(self):
        base = self.solve_case()
        fixed = [dict(p, locked=True) for p in base["layout"]["placements"]]
        again = self.solve_case(placements=fixed, auto_place=True)
        self.assertEqual(
            sorted((p["ref"], p["col"], p["row"], p["rot"]) for p in fixed),
            sorted((p["ref"], p["col"], p["row"], p["rot"])
                   for p in again["layout"]["placements"]),
        )

    def test_projeto_salvo_restaura_as_posicoes(self):
        """Reabrir um projeto exportado tem que devolver o layout identico.

        E o caminho do 'Projeto (.json)': as posicoes voltam e o sistema so
        reroteia, sem reposicionar nada.
        """
        base = self.solve_case()
        salvo = [dict(p) for p in base["layout"]["placements"]]

        volta = self.solve_case(placements=salvo, auto_place=False)
        self.assertEqual(
            sorted((p["ref"], p["col"], p["row"], p["rot"], p["locked"]) for p in salvo),
            sorted((p["ref"], p["col"], p["row"], p["rot"], p["locked"])
                   for p in volta["layout"]["placements"]),
            "reabrir o projeto salvo mudou a posicao das pecas",
        )
        self.assert_tudo(volta)

    def test_projeto_salvo_preserva_travas(self):
        base = self.solve_case()
        salvo = [dict(p, locked=(p["ref"] in ("U1", "J1"))) for p in base["layout"]["placements"]]
        volta = self.solve_case(placements=salvo, auto_place=False)
        travados = {p["ref"] for p in volta["layout"]["placements"] if p["locked"]}
        self.assertEqual(travados, {"U1", "J1"})

    # ------------------------------------------------------------------
    # tamanho real das pecas
    # ------------------------------------------------------------------

    def test_posicionador_nao_sobrepoe_corpos(self):
        """O posicionador precisa respeitar o CORPO, nao o vao dos pinos.

        Regressao: o placer media colisao pelo retangulo dos pinos enquanto a
        verificacao usava o corpo com as folgas. Ele otimizava uma coisa e era
        julgado por outra, entao entregava pecas visivelmente empilhadas.
        """
        # infla os corpos para o conflito ser inevitavel se a conta estiver errada
        overrides = {ref: {"margins": [1, 1, 1, 2]}
                     for ref in ("U1", "J1", "C1", "C2", "R1", "R2", "R3", "D1")}
        for seed in (1, 2, 3, 4):
            res = self.solve_case(
                board={"cols": 26, "rows": 22},
                overrides=overrides,
                placer={"effort": "normal", "seed": seed},
            )
            sobrepostos = [p for p in res["problems"] if "sobrepoem" in p]
            self.assertEqual(sobrepostos, [],
                             "semente %d colocou pecas uma em cima da outra: %s"
                             % (seed, sobrepostos))

    def test_placer_conta_o_mesmo_que_a_verificacao(self):
        """O que o posicionador acha que fez tem que bater com o que foi feito.

        Sem esta amarra, um erro de contabilidade volta a passar despercebido:
        o relatorio diz '0 sobreposicoes' e o desenho mostra pecas empilhadas.
        """
        overrides = {"J1": {"margins": [2, 2, 2, 2]}, "U1": {"margins": [1, 1, 1, 1]}}
        for seed in (1, 2, 3):
            res = self.solve_case(board={"cols": 26, "rows": 22}, overrides=overrides,
                                  placer={"effort": "normal", "seed": seed})
            relatado = res["placement"]["overlaps"]
            reais = len([p for p in res["problems"] if "sobrepoem" in p])
            self.assertEqual(bool(relatado), bool(reais),
                             "placer relatou %r sobreposicoes e a verificacao achou %d"
                             % (relatado, reais))

    def test_corpo_maior_que_os_pinos_e_respeitado(self):
        """Aumentar o corpo tem que mudar o desenho e a colisao, sem mexer nos pinos."""
        pequeno = self.solve_case()
        grande = self.solve_case(overrides={"J1": {"margins": [0, 0, 0, 4]}},
                                 auto_place=False,
                                 placements=pequeno["layout"]["placements"])

        fp_p = pequeno["layout"]["footprints"]["J1"]
        fp_g = grande["layout"]["footprints"]["J1"]
        self.assertEqual(fp_p["pins"], fp_g["pins"], "os pinos nao podiam mudar")
        self.assertGreater(fp_g["body_size"][1], fp_p["body_size"][1],
                           "o corpo deveria ter ficado mais fundo")
        self.assertEqual(fp_g["margins"], [0, 0, 0, 4])
        # e as outras pecas seguem intactas
        self.assertEqual(pequeno["layout"]["footprints"]["U1"]["margins"],
                         grande["layout"]["footprints"]["U1"]["margins"])

    def _furos(self, res, ref):
        return sorted((p["pin"], p["col"], p["row"])
                      for p in res["layout"]["pins"] if p["ref"] == ref)

    @staticmethod
    def _vao(furos):
        """Maior distancia entre furos, em qualquer direcao.

        A peca pode estar girada: medir so as colunas daria zero para uma peca
        em pe, e o teste passaria sem testar nada.
        """
        cols = [c for _p, c, _r in furos]
        rows = [r for _p, _c, r in furos]
        return max(max(cols) - min(cols), max(rows) - min(rows))

    def test_afastamento_dos_terminais_e_ajustavel(self):
        """Um poliester de 100nF tem as pernas mais abertas que um ceramico.

        O footprint do KiCad da o passo nominal; a peca que a pessoa comprou pode
        ter outro, e ela precisa poder dizer isso.
        """
        base = self.solve_case()
        pos = [dict(p) for p in base["layout"]["placements"]]
        largo = self.solve_case(placements=pos, auto_place=False,
                                overrides={"C2": {"passo": 5}})

        antes, depois = self._furos(base, "C2"), self._furos(largo, "C2")
        self.assertEqual(self._vao(depois), 5, "o passo pedido nao chegou nos furos")
        self.assertGreater(self._vao(depois), self._vao(antes))
        self.assertEqual(len(depois), len(antes), "nao pode ganhar nem perder terminal")
        self.assertEqual(self._furos(base, "U1"), self._furos(largo, "U1"),
                         "mexer numa peca nao pode mexer nas outras")
        self.assert_tudo(largo)

    def test_passo_maior_mantem_a_numeracao_dos_pinos(self):
        """Abrir as pernas nao pode trocar pino 1 com pino 2 - inverteria a peca.

        Num DIP isso seria pior ainda: alargar a pastilha trocaria a pinagem
        inteira, e o circuito montado ficaria errado sem nenhum aviso.
        """
        from perfboard.footprints import arranjo_dos_pinos, infer, aplica_override

        casos = [
            ("Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm", ["1", "2"], {"passo": 6}),
            ("Package_TO_SOT_THT:TO-92_Inline", ["1", "2", "3"], {"passo": 2}),
            ("Package_DIP:DIP-8_W7.62mm", [str(i) for i in range(1, 9)], {"largura": 6}),
        ]
        for fp, pinos, ajuste in casos:
            antes = infer(fp, pinos, "X1")
            depois = aplica_override(infer(fp, pinos, "X1"), dict(ajuste))
            self.assertEqual(set(antes.pins), set(depois.pins), fp)

            # a ordem relativa dos pinos em cada eixo tem que ser a mesma
            def ordem(d):
                return sorted(d.pins, key=lambda k: (d.pins[k][1], d.pins[k][0]))
            self.assertEqual(ordem(antes), ordem(depois),
                             "%s: a numeracao mudou de lugar" % fp)

            a = arranjo_dos_pinos(depois.pins)
            for chave, valor in ajuste.items():
                self.assertEqual(a[chave], valor, "%s: %s nao foi aplicado" % (fp, chave))

    def test_padrao_irregular_nao_aceita_passo(self):
        """Sem fileira regular nao existe passo unico: melhor recusar que inventar."""
        from perfboard.footprints import FootprintDef, redistribui_pinos

        torto = FootprintDef(key="x", label="torto",
                             pins={"1": (0, 0), "2": (3, 1), "3": (1, 5)})
        self.assertFalse(redistribui_pinos(torto, passo=2))
        self.assertEqual(torto.pins, {"1": (0, 0), "2": (3, 1), "3": (1, 5)},
                         "recusou, entao nao podia ter mexido")

    def test_corpo_gira_junto_com_a_peca(self):
        """Uma peca funda girada 90 graus fica larga, nao funda."""
        base = self.solve_case(overrides={"J1": {"margins": [0, 0, 0, 3]}})
        pos = [dict(p, rot=0) if p["ref"] == "J1" else dict(p)
               for p in base["layout"]["placements"]]
        reto = self.solve_case(overrides={"J1": {"margins": [0, 0, 0, 3]}},
                               auto_place=False, placements=pos)
        girado_pos = [dict(p, rot=90) if p["ref"] == "J1" else dict(p) for p in pos]
        girado = self.solve_case(overrides={"J1": {"margins": [0, 0, 0, 3]}},
                                 auto_place=False, placements=girado_pos)

        def caixa(res):
            xs = [p["col"] for p in res["layout"]["pins"] if p["ref"] == "J1"]
            return xs

        # o tamanho do corpo em furos e o mesmo; o que muda e a orientacao na placa
        fp_r = reto["layout"]["footprints"]["J1"]["body_size"]
        fp_g = girado["layout"]["footprints"]["J1"]["body_size"]
        self.assertEqual(fp_r, fp_g, "o footprint em si nao muda ao girar")
        self.assertNotEqual(caixa(reto), caixa(girado),
                            "girar 90 graus tinha que mudar a ocupacao na placa")

    def test_corpo_sobrando_na_borda_nao_e_defeito(self):
        """Borne na borda tem o corpo para fora da placa - e assim que se monta.

        Do lado de fora nao ha nada com que colidir, entao isso nao entra em
        `problems()`. Ja custou uma busca que nunca parava: com o corpo fora
        contando como defeito, um resultado 100% ligado era recusado para sempre
        quando o usuario travava os bornes na borda.
        """
        nl = parse_netlist(self.text)
        lib = build_library(nl, {"J1": {"margins": [0, 3, 0, 0]}})
        spec = BoardSpec.from_json(self.analysis["suggested_board"])
        lay = build_layout(nl, spec, lib, None)

        pl = lay.placements["J1"]
        pl.row, pl.rot = 0, 0        # pinos na primeira linha, corpo passando por cima

        self.assertGreater(lay.body_off_board("J1"), 0,
                           "o teste so vale se o corpo de J1 realmente passar da borda")
        self.assertEqual(lay.out_of_board("J1"), 0, "os pinos tinham que caber")
        self.assertEqual([m for m in lay.problems() if m.startswith("J1")], [],
                         "corpo sobrando na borda nao e defeito")

    # Placa larga com duas pecas soltas: R1 na primeira linha com o corpo passando
    # por cima da borda - a situacao do borne travado na beirada.
    MINI_CASO = {
        "board": {"cols": 10, "rows": 10},
        "placements": [
            {"ref": "R1", "col": 1, "row": 0, "rot": 0, "locked": True},
            {"ref": "R2", "col": 1, "row": 5, "rot": 0, "locked": True}],
        "overrides": {"R1": {"margins": [0, 3, 0, 0]}},
    }

    def mini_solve(self, freio=None, **placer):
        payload = dict(self.MINI_CASO, netlist=MINI_NET)
        payload["placer"] = dict({"effort": "rapido", "seed": 3, "tries": 0}, **placer)
        return solve(payload, cancelado=freio)

    def test_modo_primeiro_para_com_corpo_na_borda(self):
        """Pedindo para parar na primeira, ele para na primeira - mesmo com o corpo
        de R1 passando da borda.

        O laco so pode continuar por falta de ligacao, curto ou pino fora da placa.
        Se corpo na borda voltar a contar como defeito, isto roda ate o freio.
        """
        vezes = []

        def freio():
            vezes.append(1)
            return len(vezes) > 3      # rede de seguranca contra laco infinito

        res = self.mini_solve(freio=freio, modo="primeiro")
        self.assertTrue(res["ok"], "layout bom: tinha que fechar. Problemas: %s"
                        % res["problems"])
        self.assertEqual(res["problems"], [])
        self.assertEqual(res["placement"]["tries"], 1,
                         "fechou na primeira: nao podia ter pedido outra tentativa")

    def test_modo_otimizar_continua_e_para_no_plato(self):
        """No modo normal ele nao para na primeira: segue caçando algo mais facil de
        montar e para sozinho quando a melhora estaciona.

        Com as duas pecas travadas nao ha o que melhorar, entao o platô chega no
        limite de paciencia - e e ele quem encerra, nao o freio.
        """
        vezes = []

        def freio():
            vezes.append(1)
            return len(vezes) > 200

        res = self.mini_solve(freio=freio, modo="otimizar", paciencia=3)
        self.assertTrue(res["ok"], res["problems"])
        self.assertTrue(res["placement"]["plato"], "tinha que ter declarado platô")
        self.assertGreater(res["placement"]["tries"], 1,
                           "no modo otimizar ele nao para na primeira")
        self.assertLessEqual(res["placement"]["tries"], 200,
                             "quem encerrou tinha que ser o platô, nao o freio")
        self.assertTrue(all(p["fechou"] for p in res["historico"]),
                        "neste caso trivial toda tentativa fecha")

    def test_busca_em_paralelo_entrega_resultado_integro(self):
        """Tentativas em processos separados nao podem desencontrar layout e rota.

        O risco do paralelismo e entregar o desenho de uma tentativa com as
        estatisticas de outra. `assert_tudo` confere que cada pino do layout
        devolvido esta realmente ligado pelas rotas devolvidas, entao um
        desencontro aparece aqui.
        """
        res = solve({"netlist": self.text,
                     "board": self.analysis["suggested_board"],
                     "placer": {"effort": "rapido", "seed": 5, "tries": 0,
                                "modo": "primeiro", "nucleos": 3}})
        self.assertEqual(res["placement"]["nucleos"], 3)
        self.assertTrue(res["ok"], res["problems"])
        self.assert_tudo(res)
        self.assertEqual(res["historico"][-1]["tentativa"], res["placement"]["tries"])
        self.assertTrue(res["historico"][-1]["fechou"])

    def test_paralelo_e_sequencial_valem_o_mesmo(self):
        """Mudar o numero de processos e detalhe de execucao, nao de resultado.

        Nao exigimos o MESMO layout - cada tentativa e um sorteio e a ordem de
        chegada muda. Exigimos que os dois caminhos entreguem algo completo e
        legal, com o mesmo conjunto de pecas.
        """
        um = solve({"netlist": self.text, "board": self.analysis["suggested_board"],
                    "placer": {"effort": "rapido", "seed": 2, "tries": 0,
                               "modo": "primeiro", "nucleos": 1}})
        varios = solve({"netlist": self.text, "board": self.analysis["suggested_board"],
                        "placer": {"effort": "rapido", "seed": 2, "tries": 0,
                                   "modo": "primeiro", "nucleos": 4}})
        for res in (um, varios):
            self.assertTrue(res["ok"], res["problems"])
            self.assert_tudo(res)
        self.assertEqual(
            sorted(p["ref"] for p in um["layout"]["placements"]),
            sorted(p["ref"] for p in varios["layout"]["placements"]))

    def test_jumper_nunca_pousa_em_furo_com_pino(self):
        """Um furo comporta o terminal OU o fio do jumper, nunca os dois.

        Na bancada nao da para enfiar a ponta do jumper no mesmo furo que ja tem a
        perna do CI: puxa-se uma trilha curta ate um furo vago ao lado. O roteador
        precisa produzir esse caminho, e nao encostar o jumper no pino.
        """
        ocupados = set()
        for faces in (1, 2):
            for seed in (1, 2, 3, 4):
                res = self.solve_case(router={"faces": faces, "allow_jumpers": True},
                                      placer={"effort": "rapido", "seed": seed})
                ocupados = {(p["col"], p["row"]) for p in res["layout"]["pins"]}
                for rota in res["routes"]:
                    for seg in rota["segments"]:
                        if seg["type"] != "jumper":
                            continue
                        for ponta in (tuple(seg["from"]), tuple(seg["to"])):
                            self.assertNotIn(
                                ponta, ocupados,
                                "jumper da rede %s pousou em %s, que ja tem terminal"
                                % (rota["name"], ponta))
                self.assert_tudo(res)
        self.assertTrue(ocupados, "o teste precisa ter visto algum pino")

    # ------------------------------------------------------------------
    # desacoplamento
    # ------------------------------------------------------------------

    def test_desacoplamento_nunca_poe_o_capacitor_sobre_o_ci(self):
        """Aproximar o capacitor jamais justifica enfia-lo dentro de outra peca.

        Regressao: o premio do desacoplamento (45/furo) era maior que a penalidade de
        sobreposicao (40/furo), entao o posicionador COMPRAVA a sobreposicao e punha o
        capacitor em cima do CI - o que so seria montavel com a peca do outro lado da
        placa, coisa que o programa nem modela.
        """
        from perfboard.project import sugere_desacoplamento
        from perfboard.netlist import parse_netlist
        with open(os.path.join(HERE, "..", "examples", "registrador_595.net"),
                  encoding="utf-8") as fh:
            texto = fh.read()
        par = sugere_desacoplamento(parse_netlist(texto))[0]

        for seed in (1, 2, 3, 4, 5):
            res = solve({"netlist": texto, "board": {"cols": 22, "rows": 20},
                         "placer": {"effort": "normal", "seed": seed},
                         "decoupling": [par]})
            sobrepostos = [p for p in res["problems"] if "sobrepoem" in p]
            self.assertEqual(sobrepostos, [],
                             "semente %d empilhou pecas para encurtar o laco: %s"
                             % (seed, sobrepostos))

    def test_detecta_capacitor_de_desacoplamento(self):
        """C2 liga CTRL a GND no 555: nao e desacoplamento. C do 595 e."""
        from perfboard.project import sugere_desacoplamento
        from perfboard.netlist import parse_netlist
        outro = os.path.join(HERE, "..", "examples", "registrador_595.net")
        with open(outro, encoding="utf-8") as fh:
            pares = sugere_desacoplamento(parse_netlist(fh.read()))
        self.assertTrue(pares, "o 100n entre VCC e GND do 74HC595 deveria ser detectado")
        p = pares[0]
        self.assertEqual((p["cap"], p["ic"]), ("C1", "U1"))
        self.assertTrue(p["net_pwr"].upper().find("GND") < 0)
        self.assertIn("GND", p["net_gnd"].upper())

    def test_desacoplamento_aproxima_de_verdade(self):
        """Com a regra ligada, o capacitor tem que encostar no pino do CI."""
        with open(os.path.join(HERE, "..", "examples", "registrador_595.net"),
                  encoding="utf-8") as fh:
            texto = fh.read()

        def laco(res, par):
            """O que a regra otimiza e o LACO inteiro, nao uma perna sozinha.

            Comparar so a alimentacao engana: encurtar o laco pode alongar um lado
            para encurtar mais o outro.
            """
            pos = {(x["ref"], x["pin"]): (x["col"], x["row"]) for x in res["layout"]["pins"]}
            total = 0
            for pa, pb in ((par["cap_pin_pwr"], par["ic_pin_pwr"]),
                           (par["cap_pin_gnd"], par["ic_pin_gnd"])):
                a, b = pos[(par["cap"], str(pa))], pos[(par["ic"], str(pb))]
                total += abs(a[0] - b[0]) + abs(a[1] - b[1])
            return total

        from perfboard.project import sugere_desacoplamento
        from perfboard.netlist import parse_netlist
        par = sugere_desacoplamento(parse_netlist(texto))[0]

        base = {"netlist": texto, "board": {"cols": 22, "rows": 20},
                "placer": {"effort": "normal", "seed": 2}}

        solto = solve(dict(base, decoupling=[]))
        colado = solve(dict(base, decoupling=[par]))

        rel = colado["decoupling"][0]
        # Num DIP-16 o VCC e o GND ficam em cantos opostos, entao o laco tem um piso
        # fisico. O que exigimos e chegar perto DESSE piso, nao de zero.
        self.assertLessEqual(rel["excesso_furos"], 4,
                             "laco de %d furos com piso teorico de %d: longe demais"
                             % (rel["laco_furos"], rel["piso_furos"]))
        self.assertTrue(rel["ok"])
        self.assertIn("pwr_mm", rel)

        # e a regra tem que encurtar o laco de fato
        self.assertLess(laco(colado, par), laco(solto, par),
                        "com a regra ligada o laco tinha que ficar menor: %d vs %d"
                        % (laco(colado, par), laco(solto, par)))

    def test_desacoplamento_desligado_nao_restringe(self):
        res = self.solve_case(decoupling=[])
        self.assertEqual(res["decoupling"], [])
        self.assert_tudo(res)

    def test_placa_pequena_demais_nao_quebra(self):
        res = self.solve_case(board={"cols": 6, "rows": 5})
        self.assertFalse(res["ok"], "placa impossivel deveria ser reprovada")
        self.assertTrue(res["problems"] or res["shorts"] or res["stats"]["failed"],
                        "o relatorio precisa dizer o que deu errado")
        self.assert_legal(res)

    def test_svg_gerado(self):
        res = self.solve_case()
        self.assertTrue(res["svg_top"].startswith("<svg"))
        self.assertIn('data-side="bottom"', res["svg_bottom"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
