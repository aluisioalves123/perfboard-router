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

    def test_nome_do_furo_segue_a_numeracao_da_placa(self):
        """A placa de quem monta manda no nome do furo.

        Nem toda perfboard comeca no canto superior esquerdo. Se o guia disser
        "R6 no furo Q12" e a placa tiver outro nome impresso ali, a montagem vira
        adivinhacao - foi exatamente o que aconteceu com uma placa de duas metades
        coladas, onde a numeracao corre ao contrario.
        """
        from perfboard.board import BoardSpec, rotulador

        spec = BoardSpec(24, 18)
        cantos = {}
        for origem in ("TL", "TR", "BL", "BR"):
            nome = rotulador(BoardSpec(24, 18, label_origin=origem))
            cantos[origem] = nome(0, 0)
        self.assertEqual(cantos["TL"], "A1")
        self.assertEqual(cantos["TR"], "A24", "coluna deveria contar da direita")
        self.assertEqual(cantos["BL"], "R1", "linha deveria contar de baixo")
        self.assertEqual(cantos["BR"], "R24")

        # o furo A1 existe uma vez so, em cada convencao
        for origem in ("TL", "TR", "BL", "BR"):
            nome = rotulador(BoardSpec(24, 18, label_origin=origem))
            todos = [nome(c, r) for c in range(24) for r in range(18)]
            self.assertEqual(len(set(todos)), len(todos), "nome repetido em %s" % origem)
            self.assertIn("A1", todos)

    def test_origem_muda_o_nome_mas_nao_o_layout(self):
        """Trocar o canto e questao de etiqueta: a placa fisica nao pode mudar."""
        base = self.solve_case()
        pos = [dict(p) for p in base["layout"]["placements"]]
        placa = dict(self.analysis["suggested_board"])

        virado = self.solve_case(board=dict(placa, label_origin="TR"),
                                 placements=pos, auto_place=False)
        normal = self.solve_case(board=dict(placa, label_origin="TL"),
                                 placements=pos, auto_place=False)

        def furos(res):
            return sorted((p["ref"], p["pin"], p["col"], p["row"])
                          for p in res["layout"]["pins"])

        self.assertEqual(furos(normal), furos(virado), "a geometria nao podia mudar")
        self.assertEqual(normal["stats"]["total_mm"], virado["stats"]["total_mm"])

        # ja os nomes tem que mudar, senao a opcao nao serve para nada
        def nomes(res):
            return [p["label"] for p in sorted(res["layout"]["pins"],
                                               key=lambda p: (p["ref"], p["pin"]))]
        self.assertNotEqual(nomes(normal), nomes(virado))

        # e o guia de montagem tem que falar a MESMA lingua do desenho
        cols = placa["cols"]
        for res, origem in ((normal, "TL"), (virado, "TR")):
            por_furo = {(p["col"], p["row"]): p["label"] for p in res["layout"]["pins"]}
            for peca in res["build"]["components"]:
                for pino, rotulo in peca["pin_labels"].items():
                    self.assertIn(rotulo, por_furo.values(),
                                  "guia de montagem em %s citou furo que o desenho nao tem"
                                  % origem)

    def _de_cima(self, res):
        return [s for r in res["routes"] for s in r["segments"] if s.get("layer") == 1]

    def _layout_de_teste(self):
        from perfboard.board import BoardSpec
        from perfboard.project import build_layout
        from perfboard.placer import auto_place
        from perfboard import footprints as fpmod
        from perfboard.netlist import parse_netlist

        nl = parse_netlist(self.text)
        lib = fpmod.build_library(nl, {})
        spec = BoardSpec.from_json(self.analysis["suggested_board"])
        lay = build_layout(nl, spec, lib, None)
        auto_place(lay, nl, seed=3, effort="rapido")
        return nl, lay

    FACE_NUM = {"solda": 0, "componentes": 1}

    @staticmethod
    def _passos(a, b):
        """Quebra um trecho reto nos passos de furo a furo que ele cobre."""
        dx = (b[0] > a[0]) - (b[0] < a[0])
        dy = (b[1] > a[1]) - (b[1] < a[1])
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        saida = []
        for k in range(n):
            u = (a[0] + dx * k, a[1] + dy * k)
            v = (a[0] + dx * (k + 1), a[1] + dy * (k + 1))
            saida.append((min(u, v), max(u, v)))
        return saida

    def _plano(self, **kw):
        res = self.solve_case(router=dict({"faces": 2, "allow_jumpers": True}, **kw))
        return res, res["build"]["bancada"]

    def test_duas_pontas_nunca_dividem_o_furo_nem_apos_recuar(self):
        """Recuar uma ponta pode joga-la em cima de OUTRA ponta, um furo adiante.

        Regressao achada na placa do usuario: o separador rodava uma passada so,
        entao o conflito criado pelo proprio recuo passava batido. Aqui os fios sao
        montados de proposito para que o primeiro recuo gere o segundo conflito.
        """
        from perfboard.bancada import _separa_pontas

        # tres fios terminando em furos vizinhos, em fila: ao recuar, um cai no outro
        segs = [((0, 0), (4, 0)), ((4, 0), (4, 4)), ((3, 0), (3, 5))]
        fios, _pontes = _separa_pontas(segs)
        pontas = [p for f in fios for p in f]
        self.assertEqual(len(pontas), len(set(pontas)),
                         "sobrou furo com duas pontas: %s" % pontas)

    def test_ponta_de_fio_nunca_cai_onde_ja_passa_outro_fio(self):
        """Nao se solda a ponta de um fio em cima de outro fio deitado na ilha.

        E o mesmo caso do terminal, e demorei a enxergar: eu tratava "um fio
        comprido e outro que se encontra ali" como contato direto, quando o que
        acontece e o segundo parar um furo antes e uma ponte de solda ligar os dois.
        Achado na placa do usuario, nos furos N8 e D14.
        """
        from perfboard.bancada import _celulas

        res, plano = self._plano(faces=2, allow_jumpers=True)
        for face in ("solda", "componentes"):
            daqui = [f for f in plano["fios"] if f["face"] == face]
            ocupa = {}
            for i, f in enumerate(daqui):
                for c in _celulas(tuple(f["de"]), tuple(f["ate"])):
                    ocupa.setdefault(c, set()).add(i)
            for i, f in enumerate(daqui):
                for ponta, cruza in ((tuple(f["de"]), f["de_atravessa"]),
                                     (tuple(f["ate"]), f["ate_atravessa"])):
                    if cruza:
                        continue
                    self.assertFalse(
                        ocupa.get(ponta, set()) - {i},
                        "fio de %s termina em %s, onde ja passa outro fio"
                        % (f["net"], f["de_label"]))

    def test_o_programa_confere_a_propria_montagem(self):
        """A conferencia das regras de bancada roda sozinha e sai limpa.

        Existe para o usuario nao ter de cacar isso a olho no desenho - foi assim
        que os erros anteriores apareceram, e custou caro aos dois lados.
        """
        res, _plano = self._plano(faces=2, allow_jumpers=True)
        self.assertEqual(res["montagem_problemas"], [])

    def test_fio_nunca_termina_em_furo_de_terminal(self):
        """Nada conecta ao pino a nao ser estanho.

        Na bancada o fio nao encosta no terminal: para um furo antes, e uma ponte
        de solda faz a ligacao. O desenho mostrava o fio chegando no pino, que e
        uma coisa que ninguem consegue montar.
        """
        res, plano = self._plano(faces=2, allow_jumpers=True)
        pinos = {(p["col"], p["row"]) for p in res["layout"]["pins"]}
        self.assertTrue(pinos)

        for f in plano["fios"]:
            for ponta, atravessa in ((tuple(f["de"]), f["de_atravessa"]),
                                     (tuple(f["ate"]), f["ate_atravessa"])):
                if atravessa:
                    continue        # ali o fio nao termina: passa para o outro lado
                self.assertNotIn(ponta, pinos,
                                 "fio de %s termina no terminal %s - deveria parar "
                                 "um furo antes e ligar com solda"
                                 % (f["net"], f["de_label"]))

    def test_todo_pino_ligado_aparece_como_ponto_de_solda(self):
        """Onde a fiacao encosta num terminal, tem de haver solda marcada.

        Fio PASSANDO por cima de um pino esta liberado: ele deita sobre a ilha e o
        estanho ali faz a ligacao - que continua sendo solda conectando o terminal,
        nunca o fio encostando por conta propria. O que nao pode e o fio TERMINAR
        no pino, e disso cuida o teste acima.
        """
        res, plano = self._plano(faces=2, allow_jumpers=True)
        pinos = {(p["col"], p["row"]) for p in res["layout"]["pins"]}

        def celulas(x):
            a, b = tuple(x["de"]), tuple(x["ate"])
            d = ((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
            n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
            return [(a[0] + d[0] * k, a[1] + d[1] * k) for k in range(n + 1)]

        tocados = {c for x in plano["fios"] + plano["pontes"]
                   for c in celulas(x) if c in pinos}
        marcados = {tuple(j["furo"]) for j in plano["juntas"] if j["formato"] == "pino"}
        self.assertEqual(tocados - marcados, set(),
                         "pino tocado pela fiacao sem solda marcada: %s"
                         % sorted(tocados - marcados))

    def test_via_nao_e_uma_segunda_ponta_de_fio_no_furo(self):
        """Via e o proprio fio atravessando o furo, nao outro pedaco.

        Regressao achada na bancada: 23 furos tinham a ponta de um fio dividindo o
        buraco com uma via - e via tambem e fio. Duas pontas no mesmo furo, que e
        justamente o que nao existe. Na pratica passa-se a ponta do proprio fio
        pelo furo e segue do outro lado.
        """
        res, plano = self._plano(faces=2, allow_jumpers=False)
        vias = {}
        for rota in res["routes"]:
            for seg in rota["segments"]:
                if seg["type"] == "via":
                    vias.setdefault(rota["name"], set()).add(tuple(seg["from"]))
        if not any(vias.values()):
            self.skipTest("este layout nao usou via")

        for f in plano["fios"]:
            das_minhas = vias.get(f["net"], set())
            for ponta, atravessa in ((tuple(f["de"]), f["de_atravessa"]),
                                     (tuple(f["ate"]), f["ate_atravessa"])):
                if ponta in das_minhas:
                    self.assertTrue(atravessa,
                                    "fio de %s termina em %s, que tem via: deveria "
                                    "atravessar" % (f["net"], f["de_label"]))

    def test_travessia_aparece_como_via_e_nao_como_fim_de_fio(self):
        """Onde o fio passa para o outro lado, quem marca o ponto e a via.

        Nao existe marcador de "ponta de fio" no desenho: depois das regras da
        bancada, toda ponta cai onde ja ha solda desenhada - ponte, junta ou
        travessia. Marcar de novo era inventar um terceiro tipo de ponto.
        """
        res, plano = self._plano(faces=2, allow_jumpers=False)
        self.assertEqual(res["svg_bottom"].count("solda na ponta do fio"), 0)
        self.assertEqual(res["svg_top"].count("solda na ponta do fio"), 0)

        travessias = [f for f in plano["fios"]
                      if f["de_atravessa"] or f["ate_atravessa"]]
        if travessias:
            # cada travessia tem de ter uma via desenhada naquele furo
            vias = {tuple(s["from"]) for r in res["routes"] for s in r["segments"]
                    if s["type"] == "via"}
            for f in travessias:
                for ponta, cruza in ((tuple(f["de"]), f["de_atravessa"]),
                                     (tuple(f["ate"]), f["ate_atravessa"])):
                    if cruza:
                        self.assertIn(ponta, vias)

    def test_pino_tocado_pela_fiacao_e_ponto_de_solda(self):
        """O furo do terminal tambem leva ferro - e ali que a peca entra na rede.

        Marcavamos junta so onde dois fios se encontram, entao um fio passando por
        cima de um pino nao aparecia como solda em lugar nenhum.
        """
        res, plano = self._plano()
        pinos = {(p["col"], p["row"]): "%s.%s" % (p["ref"], p["pin"])
                 for p in res["layout"]["pins"]}

        em_pino = [j for j in plano["juntas"] if j["formato"] == "pino"]
        self.assertTrue(em_pino, "nenhuma solda em pino foi marcada")
        for j in em_pino:
            furo = tuple(j["furo"])
            self.assertIn(furo, pinos, "junta 'pino' num furo que nao tem terminal")
            self.assertEqual(j["pino"], pinos[furo])

        # todo furo de pino coberto pela fiacao tem de estar marcado
        def celulas(x):
            a, b = tuple(x["de"]), tuple(x["ate"])
            dx = (b[0] > a[0]) - (b[0] < a[0])
            dy = (b[1] > a[1]) - (b[1] < a[1])
            n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
            return [(a[0] + dx * k, a[1] + dy * k) for k in range(n + 1)]

        marcados = {tuple(j["furo"]) for j in em_pino}
        for x in plano["fios"] + plano["pontes"]:
            for c in celulas(x):
                if c in pinos:
                    self.assertIn(c, marcados,
                                  "fiacao passa pelo pino %s e nao ha solda marcada"
                                  % pinos[c])

    def test_juncao_em_T_e_um_fio_comprido_mais_um_ramo(self):
        """Tres fios nao se encontram num furo - isso nao existe na bancada.

        Numa derivacao o que se faz e UM fio passando reto e outro encontrando ele
        ali. Se o roteador quebrou a reta em dois pedacos por causa da derivacao,
        cabe ao guia junta-los de volta: montar em dois seria cortar dois fios e
        emendar no meio.
        """
        from perfboard.bancada import _funde_colineares, _separa_pontas

        # reta de (0,0) a (6,0) partida na derivacao, mais o ramo subindo
        segs = [((0, 0), (3, 0)), ((3, 0), (6, 0)), ((3, 0), (3, 4))]
        fios, pontes = _separa_pontas(_funde_colineares(segs))

        comprido = max(fios, key=lambda t: max(abs(t[1][0] - t[0][0]),
                                               abs(t[1][1] - t[0][1])))
        self.assertEqual(sorted(comprido), [(0, 0), (6, 0)],
                         "a reta tinha de voltar a ser um fio so")
        # O ramo NAO encosta na reta: para um furo antes e o estanho liga. Soldar a
        # ponta de um fio em cima de outro fio deitado nao existe na bancada.
        self.assertTrue(pontes, "o ramo deveria chegar na reta por ponte de solda")
        for a, b in pontes:
            self.assertEqual(max(abs(b[0] - a[0]), abs(b[1] - a[1])), 1)

    def test_quina_vira_fio_mais_ponte(self):
        """Quina nao e fusao de dois fios: um vai ate ela, o outro comeca ao lado."""
        from perfboard.bancada import _funde_colineares, _separa_pontas

        segs = [((0, 0), (3, 0)), ((3, 0), (3, 4))]
        fios, pontes = _separa_pontas(_funde_colineares(segs))
        self.assertEqual(len(pontes), 1, "faltou a ponte de solda da quina")
        a, b = pontes[0]
        self.assertEqual(max(abs(b[0] - a[0]), abs(b[1] - a[1])), 1)
        pontas = [p for f in fios for p in f]
        self.assertEqual(len(pontas), len(set(pontas)), "duas pontas no mesmo furo")

    def test_reta_partida_volta_a_ser_um_fio(self):
        """Dois pedacos em linha reta sao um fio so, nao dois emendados."""
        from perfboard.bancada import _funde_colineares

        self.assertEqual(len(_funde_colineares([((0, 0), (3, 0)), ((3, 0), (6, 0))])), 1)
        # quina NAO funde: os pedacos mudam de eixo
        self.assertEqual(len(_funde_colineares([((0, 0), (3, 0)), ((3, 0), (3, 4))])), 2)

    def _coord_de_rotulo(self, res):
        """rotulo do furo -> (coluna, linha), como o guia numera a placa."""
        from perfboard.board import BoardSpec, rotulador
        spec = BoardSpec.from_json(res["board"])
        nome = rotulador(spec, res.get("label_style", "letra"))
        return {nome(c, r): (c, r)
                for c in range(spec.cols) for r in range(spec.rows)}

    def test_montar_seguindo_o_manual_da_o_circuito_da_netlist(self):
        """A prova final: montar no papel so com o que o texto manda fazer.

        As conferencias antigas olhavam o PLANO - e o plano estava certo. O que
        estava errado era o que o manual mandava fazer com ele, e por isso tres
        defeitos passaram: 8 pontes que nao apareciam em passo nenhum, 16 vias
        nunca mencionadas e pontes sem dizer de que face. Nenhum era visivel no
        plano; todos partem uma rede aqui.
        """
        from perfboard import footprints as fpmod
        from perfboard.netlist import parse_netlist
        from tests.montagem_no_papel import problemas, pinos_so_por_baixo

        nl = parse_netlist(self.text)
        so_por_baixo = pinos_so_por_baixo(fpmod.build_library(nl, {}))

        for faces in (1, 2):
            for seed in (1, 2, 3, 4, 5):
                res = self.solve_case(placer={"effort": "rapido", "seed": seed},
                                      router={"faces": faces})
                achados = problemas(res["build"]["roteiro"],
                                    self._coord_de_rotulo(res), nl, so_por_baixo,
                                    faces=self._faces(res))
                self.assertEqual(
                    achados, [],
                    "montando pelo manual (faces=%d, semente %d) a placa nao fecha:"
                    "\n  " % (faces, seed) + "\n  ".join(achados))

    def test_via_entre_duas_pontes_vira_um_passo_de_verdade(self):
        """Via e um fio atravessando o furo - nao um vizinho para onde puxar solda.

        Regressao de bancada. Quando os dois lados da via sao pontes de solda nao
        sobra fio nenhum passando pelo furo, e a via sumia do roteiro: o guia
        mandava "puxe a ponte ate H18" e nunca dizia o que H18 era. Junto com ela
        sumia a ponte do outro lado, porque eu descartava ponte cujos dois furos
        parecessem vazios - e furo de via parece vazio.

        O caso e montado a mao porque o exemplo do repositorio e facil demais para
        produzir uma via dessas.
        """
        from perfboard.bancada import plano_de_montagem
        from perfboard.manual import monta_roteiro

        nl, lay = self._layout_de_teste()
        ref = sorted(lay.placements)[0]
        (col, row) = sorted(lay.pin_holes(ref).values())[0]

        # O caso do H18: pino -> ponte por baixo -> VIA -> ponte por cima. Os dois
        # trechos tem vao 1, entao viram PONTE e nao sobra fio nenhum passando pelo
        # furo da via - que e exatamente quando ela sumia do roteiro.
        via = (col + 1, row)
        rotas = [{
            "name": "REDE_DA_VIA", "ok": True,
            "segments": [
                {"type": "trace", "layer": 0, "from": [col, row],
                 "to": [via[0], via[1]], "length_mm": 2.54},
                {"type": "via", "layer": -1, "from": [via[0], via[1]],
                 "to": [via[0], via[1]], "length_mm": 0.0},
                {"type": "trace_top", "layer": 1, "from": [via[0], via[1]],
                 "to": [via[0] + 1, via[1]], "length_mm": 2.54},
            ],
            "holes": [],
        }]
        nome = lambda c, r: "%s%d" % (chr(ord("A") + c), r + 1)
        pinos = {(col, row): (ref, "1")}
        plano = plano_de_montagem(rotas, nome, pinos)
        roteiro = monta_roteiro(lay, plano, rotas, nome, netlist=nl)

        # 1) a via existe como passo, e diz para enfiar fio no furo
        vias = [p for p in roteiro if p["titulo"].startswith("Via no furo ")]
        self.assertEqual([p["titulo"].split()[3] for p in vias], [nome(*via)],
                         "a via de %s nao virou passo nenhum" % nome(*via))
        self.assertIn("Enfie uma sobra de terminal", vias[0]["detalhe"],
                      "o passo da via tem de mandar enfiar fio no furo")

        # 2) nenhuma ponte do plano pode faltar no roteiro
        from tests.montagem_no_papel import pontes_do_item
        ditas = set()
        for p in roteiro:
            for item in p["itens"]:
                for x, y, _face in pontes_do_item(item):
                    ditas.add(frozenset((x, y)))
        for b in plano["pontes"]:
            self.assertIn(frozenset((b["de_label"], b["ate_label"])), ditas,
                          "a ponte %s+%s nao aparece em passo nenhum do roteiro"
                          % (b["de_label"], b["ate_label"]))

        # 3) cada ponte diz de que FACE e. Numa placa de duas faces o furo tem duas
        # ilhas, e "puxe a ponte ate X" sem o lado e instrucao pela metade - na
        # placa real do projeto 23 das 121 pontes sao do lado dos componentes.
        lados = {}
        for p in roteiro:
            for item in p["itens"]:
                for x, y, face in pontes_do_item(item):
                    lados[frozenset((x, y))] = "por cima" if face else "por baixo"
        for b in plano["pontes"]:
            chave = frozenset((b["de_label"], b["ate_label"]))
            esperado = "por cima" if b["face"] == "componentes" else "por baixo"
            self.assertEqual(lados.get(chave), esperado,
                             "a ponte %s+%s e da face %s e o guia disse %r"
                             % (b["de_label"], b["ate_label"], b["face"],
                                lados.get(chave)))
        self.assertIn("por cima", set(lados.values()),
                      "este caso tinha de exercitar uma ponte do lado de cima")

        # 4) a via vem ANTES de qualquer ponte no furo dela: primeiro o fio
        # atravessa, depois se solda. O contrario e furo entupido.
        n_via = vias[0]["n"]
        for p in roteiro:
            for item in p["itens"]:
                if nome(*via) in item and item.startswith("na MESMA solda"):
                    self.assertGreaterEqual(
                        p["n"], n_via,
                        "passo %d solda %s antes da via, que so vem no passo %d"
                        % (p["n"], nome(*via), n_via))

    def test_ponte_nunca_e_anunciada_antes_do_fio_que_cai_no_furo(self):
        """Um furo, uma solda: a ponte sai da junta, nao antes dela.

        Regressao vinda da bancada: o roteiro mandava fazer a ponte F18+G18 no
        passo 3 e so no passo 10 passar o fio que termina em F18. Na pratica o furo
        ja estava cheio de estanho - para enfiar o fio era preciso reaquecer e
        limpar. Ponte de solda nao e uma operacao separada; e o estanho da propria
        junta puxado ate a ilha vizinha, feito de uma vez so.

        O teste le SO o texto do roteiro, que e o que o usuario tem na mao.
        """
        import re
        from tests.montagem_no_papel import pontes_do_item

        res = self.solve_case()
        roteiro = res["build"]["roteiro"]

        # em que passo cada furo recebe um terminal ou uma ponta de fio
        ocupado_em = {}
        for passo in roteiro:
            for item in passo["itens"]:
                m = re.match(r"pino \S+ no furo (\S+)$", item)
                if m:
                    ocupado_em.setdefault(m.group(1), []).append(passo["n"])
            if passo["titulo"].startswith("Fio de ") or passo["titulo"].startswith("Jumper de "):
                for rotulo in re.findall(r"\bde (\S+?)[ ,.]|\ba (\S+?)[ ,.]",
                                         passo["detalhe"]):
                    furo = rotulo[0] or rotulo[1]
                    if furo and re.match(r"^[A-Z]+\d+$", furo):
                        ocupado_em.setdefault(furo, []).append(passo["n"])

        cedo = []
        for passo in roteiro:
            for item in passo["itens"]:
                # as DUAS ilhas contam: a ponte poe estanho nas duas, e a que
                # recebe e justamente a que o texto nao destaca
                for x, y, _face in pontes_do_item(item):
                  for furo in (x, y):
                    for quando in ocupado_em.get(furo, ()):
                        if quando > passo["n"]:
                            cedo.append("passo %d poe estanho em %s, mas so o passo "
                                        "%d enfia o terminal ou a ponta de fio nesse "
                                        "furo" % (passo["n"], furo, quando))

        self.assertEqual(cedo, [], "ponte mandada antes da hora:\n  " + "\n  ".join(cedo))

    def test_nenhuma_ilha_e_soldada_em_dois_passos(self):
        """Pontes ligadas entre si sao um cordao de solda so, feito de uma vez.

        Regressao de bancada, encontrada em F18-G18-H18 pela face de cima: a ponte
        H18+G18 caia no passo da via e a F18+G18 no passo do fio, entao o guia
        mandava soldar G18 duas vezes - a segunda so depois que o fio chegasse.
        Reaquecer o que ja estava pronto e exatamente o que este roteiro existe
        para evitar.

        A regra que sai disso e simples de conferir: nenhuma ILHA (furo mais face)
        pode receber estanho em dois passos diferentes.
        """
        from tests.montagem_no_papel import pontes_do_item

        for faces in (1, 2):
            for seed in (1, 2, 3, 4, 5):
                res = self.solve_case(placer={"effort": "rapido", "seed": seed},
                                      router={"faces": faces})
                quando = {}
                for passo in res["build"]["roteiro"]:
                    for item in passo["itens"]:
                        for x, y, face in pontes_do_item(item):
                            for furo in (x, y):
                                quando.setdefault((furo, face), set()).add(passo["n"])
                duas = {ilha: sorted(n) for ilha, n in quando.items() if len(n) > 1}
                self.assertEqual(
                    duas, {},
                    "faces=%d semente %d: ilha soldada em mais de um passo — %s"
                    % (faces, seed, duas))

    def test_desenho_e_guia_contam_a_mesma_coisa(self):
        """O SVG e o texto tem de sair do mesmo plano, item a item.

        Ja divergiram duas vezes - nas juntas, e depois em fio e ponte, quando o
        desenho ainda vinha dos segmentos crus do roteador. Contar duas vezes a
        mesma coisa e garantia de divergir de novo.
        """
        res, plano = self._plano()
        svg = res["svg_bottom"]
        self.assertEqual(svg.count("ponte de solda"), plano["totais"]["pontes"],
                         "o desenho tem outro numero de pontes que o guia")
        das_faces = sum(1 for j in plano["juntas"] if j["face"] == "solda")
        self.assertEqual(svg.count("junta de solda"), das_faces)
        # ponta de fio nao e marcador: ela sempre cai onde ja ha solda desenhada
        self.assertEqual(svg.count("solda na ponta do fio"), 0)

    def test_um_furo_recebe_a_ponta_de_um_fio_so(self):
        """Duas pontas de fio nao entram no mesmo furo.

        Uma quina nao e a fusao de dois fios - isso nao existe na bancada. O
        primeiro fio vai ate a quina, o segundo comeca no furo VIZINHO, e uma
        ponte de solda liga os dois.
        """
        _res, plano = self._plano()
        pontas = {}
        for f in plano["fios"]:
            for ponta in (tuple(f["de"]), tuple(f["ate"])):
                chave = (f["face"], ponta)
                pontas[chave] = pontas.get(chave, 0) + 1
        repetidos = {k: v for k, v in pontas.items() if v > 1}
        self.assertEqual(repetidos, {},
                         "furo com mais de uma ponta de fio: %s" % repetidos)

    def test_o_plano_cobre_exatamente_o_que_foi_roteado(self):
        """Traduzir para fio e ponte nao pode perder nem inventar ligacao.

        E a verificacao que sustenta as outras: se o guia descreve um conjunto de
        passos diferente do que o roteador fechou, quem monta constroi outro
        circuito.
        """
        res, plano = self._plano()

        roteado = set()
        for rota in res["routes"]:
            for seg in rota["segments"]:
                if seg["type"] not in ("trace", "trace_top"):
                    continue
                for e in self._passos(tuple(seg["from"]), tuple(seg["to"])):
                    roteado.add((rota["name"], seg["layer"]) + e)

        montado = set()
        for x in plano["fios"] + plano["pontes"]:
            for e in self._passos(tuple(x["de"]), tuple(x["ate"])):
                montado.add((x["net"], self.FACE_NUM[x["face"]]) + e)

        self.assertEqual(roteado - montado, set(), "o guia perdeu ligacoes")
        self.assertEqual(montado - roteado, set(), "o guia inventou ligacoes")

    def test_ponte_e_sempre_entre_furos_vizinhos(self):
        """Ponte de solda so existe entre furos encostados - senao precisaria de fio."""
        _res, plano = self._plano()
        for b in plano["pontes"]:
            a, c = tuple(b["de"]), tuple(b["ate"])
            dist = max(abs(c[0] - a[0]), abs(c[1] - a[1]))
            self.assertEqual(dist, 1, "ponte de %s a %s tem %d furos de vao"
                             % (b["de_label"], b["ate_label"], dist))

    def test_junta_nunca_passa_de_uma_cruz(self):
        """O estanho alcanca o furo do meio e os quatro vizinhos, nao mais."""
        _res, plano = self._plano()
        for j in plano["juntas"]:
            self.assertLessEqual(len(j["toca"]), 4,
                                 "junta em %s alcancaria %d furos"
                                 % (j["furo_label"], len(j["toca"])))
            for v in j["toca"]:
                dist = max(abs(v[0] - j["furo"][0]), abs(v[1] - j["furo"][1]))
                self.assertEqual(dist, 1, "junta em %s alcancando furo distante"
                                 % j["furo_label"])
        self.assertEqual(plano["avisos"], [])

    def _pinos_so_por_baixo(self, res):
        pinos = {(p["col"], p["row"]): (p["ref"], p["pin"])
                 for p in res["layout"]["pins"]}
        fps = res["layout"]["footprints"]
        return pinos, {c for c, (ref, _) in pinos.items()
                       if fps.get(ref, {}).get("estorva_solda", True)}

    def test_pino_soldado_so_por_baixo_nao_entra_na_rede_pela_face_de_cima(self):
        """A ilha de cima de um pino de capacitor ou CI nao pertence a rede.

        Regressao encontrada na bancada, nao aqui: bloquear a PASSAGEM pela face de
        cima nao bastava. O roteador semeava as DUAS faces em todo furo de terminal
        ("o terminal liga os dois lados"), e o A* entrava por ali. Premissa falsa
        quando o terminal so pode ser soldado por baixo.

        Conferimos os furos que cada rede reivindica - e nao so os trechos
        desenhados - porque a semeadura acontece sempre, mesmo quando o caminho
        escolhido acaba nao passando ali. Testar pelo desenho passava por sorte.
        """
        for faces, jump in ((2, False), (2, True)):
            res = self.solve_case(router={"faces": faces, "allow_jumpers": jump})
            pinos, so_por_baixo = self._pinos_so_por_baixo(res)
            self.assertTrue(so_por_baixo, "o exemplo precisa ter capacitor ou CI")

            for rota in res["routes"]:
                for c, r, face in rota["holes"]:
                    if face != 1:
                        continue
                    self.assertNotIn(
                        (c, r), so_por_baixo,
                        "rede %s reivindica a ilha de cima de %s.%s, que so aceita "
                        "solda por baixo" % (rota["name"], *pinos.get((c, r), ("?", "?"))))

                for seg in rota["segments"]:
                    if seg.get("layer") != 1:
                        continue
                    for ponta in (tuple(seg["from"]), tuple(seg["to"])):
                        self.assertNotIn(
                            ponta, so_por_baixo,
                            "%s de %s encosta em %s.%s pelo lado de cima"
                            % (seg["type"], rota["name"],
                               *pinos.get(ponta, ("?", "?"))))

    def test_pino_de_capacitor_e_de_ci_nao_aceita_solda_por_cima(self):
        """A ceramica fica SOBRE os terminais; o socket do CI, idem.

        Nao e falta de espaco - nenhuma placa maior resolve. Foi o que travou uma
        montagem de verdade: o capacitor entregue prensado, com a ligacao dele
        marcada pelo lado de cima, onde nao ha como encostar o ferro.
        """
        from perfboard.router import Router, RouterConfig

        nl, lay = self._layout_de_teste()
        r = Router(lay, nl, RouterConfig.from_json({"faces": 2}))

        for ref, fp in lay.footprints.items():
            furos = set(lay.pin_holes(ref).values())
            if getattr(fp, "estorva", True):
                for furo in furos:
                    self.assertIn(furo, r.pinos_so_por_baixo,
                                  "%s deveria ligar so pelo lado da solda" % ref)
                    self.assertIn(furo, r.under_parts,
                                  "%s: o furo dele nao pode aceitar trilha em cima" % ref)
            else:
                for furo in furos:
                    self.assertNotIn(furo, r.pinos_so_por_baixo,
                                     "%s aceita solda por cima e foi bloqueado" % ref)

    def test_resistor_e_transistor_aceitam_solda_por_cima(self):
        """Sao os que deixam espaco natural e aguentam o calor - e a maioria da placa."""
        from perfboard.footprints import estorva_solda

        aceitam = [
            "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            "Diode_THT:D_DO-41_SOD81_P7.62mm_Horizontal",
            "Package_TO_SOT_THT:TO-92_Inline",
        ]
        recusam = [
            "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P2.50mm",
            "Capacitor_THT:CP_Radial_D6.3mm_P2.50mm",
            "Package_DIP:DIP-14_W7.62mm",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal",
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
        ]
        for fp in aceitam:
            self.assertFalse(estorva_solda("X1", fp), fp)
        for fp in recusam:
            self.assertTrue(estorva_solda("X1", fp), fp)

    def test_nao_ha_mais_exigencia_de_espaco_livre(self):
        """A regra geometrica saiu inteira - e nao pode voltar por acidente.

        Ela errou de tres formas antes de cair: proibindo a face de cima, exigindo
        anel de folga, e contando furos livres. Todas atrapalhavam o resistor, que
        e justamente a peca que NAO atrapalha.
        """
        from perfboard.router import Router, RouterConfig

        nl, lay = self._layout_de_teste()
        r = Router(lay, nl, RouterConfig.from_json({"faces": 2}))
        self.assertFalse(hasattr(r, "sem_ferro"),
                         "voltou a existir mapa de espaco para solda")
        self.assertFalse(hasattr(r, "_sem_espaco_para_solda"))

    def test_projeto_antigo_com_folga_negativa_ainda_desliga_o_topo(self):
        """Projeto salvo na versao anterior nao pode mudar de comportamento."""
        from perfboard.router import RouterConfig

        self.assertFalse(RouterConfig.from_json({"faces": 2, "folga_solda": -1}).usa_topo)
        self.assertTrue(RouterConfig.from_json({"faces": 2, "folga_solda": 2}).usa_topo)
        self.assertFalse(RouterConfig.from_json({"faces": 2, "trilha_em_cima": False}).usa_topo)
        self.assertTrue(RouterConfig.from_json({"faces": 2}).usa_topo)

    def test_sem_face_de_cima_e_sem_jumper_nao_inventa_ligacao(self):
        """Combinacao sem saida devolve pino solto, nunca ligacao imaginaria."""
        res = self.solve_case(router={"faces": 2, "allow_jumpers": False,
                                      "usa_topo": False})
        self.assertEqual(self._de_cima(res), [])
        self.assert_tudo(res)
        if res["stats"]["orphan_pins"]:
            self.assertFalse(res["ok"])
            self.assertIn("SEM LIGACAO", res["svg_top"])

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

    def test_espalhar_as_pecas_vale_no_motor_que_esta_rodando(self):
        """Sobra de placa nao economiza nada: se ha espaco, ele tem que ser usado.

        Minimizar comprimento de fio, sozinho, junta tudo num canto - e numa
        perfboard isso e o pior resultado possivel, porque a placa ja foi cortada
        nesse tamanho e o aperto so atrapalha o ferro, a trilha de cima e a via.

        Regressao dupla, e a segunda e a que doi: o custo de amontoar nasceu so no
        Python, mas quem posiciona de verdade e o nucleo C. O termo existia, estava
        no codigo, aparecia na revisao - e nao fazia absolutamente nada. Este teste
        mede o resultado pelo motor que estiver ativo, seja qual for.
        """
        import perfboard.placer as placer

        COLS, ROWS = 30, 24

        def regioes_usadas(peso, seed):
            placer.W_DENSIDADE = peso
            res = self.solve_case(board={"cols": COLS, "rows": ROWS},
                                  placer={"effort": "normal", "seed": seed})
            return len({(min(2, p["col"] * 3 // COLS), min(2, p["row"] * 3 // ROWS))
                        for p in res["layout"]["pins"]})

        antes = placer.W_DENSIDADE
        try:
            # peso alto de proposito: aqui se mede se o termo CHEGA no motor, nao
            # a calibragem fina, que e escolhida contra placas reais
            amontoado = sum(regioes_usadas(0.0, s) for s in (1, 2, 3))
            espalhado = sum(regioes_usadas(40.0, s) for s in (1, 2, 3))
        finally:
            placer.W_DENSIDADE = antes

        self.assertGreater(
            espalhado, amontoado,
            "o custo de amontoar nao mudou nada: %d regioes ocupadas com peso alto "
            "contra %d sem peso nenhum - provavelmente o motor em uso ignora o termo"
            % (espalhado, amontoado))

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

    def test_peca_axial_ganha_folga_para_a_dobra(self):
        """Resistor de 1/4W tem que assentar em 4 furos, nao em 3.

        O passo do footprint do KiCad e o da PCB, onde a dobra e feita por maquina
        rente ao corpo. Um DIN0207 em 3 furos deixa 0,6 mm entre o corpo e o furo:
        o terminal sai forcando e a peca nao assenta. A regra sai do comprimento do
        corpo mais a folga da dobra dos dois lados.
        """
        from perfboard.footprints import infer

        def vao(fp):
            d = infer(fp, ["1", "2"], "X1")
            return max(x for x, _y in d.pins.values())

        # o caso que motivou a regra: 1/4W nominal 7,62 mm
        self.assertEqual(vao("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal"), 4)
        # ja folgado, fica como esta
        self.assertEqual(vao("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"), 4)
        # corpo maior pede mais espaco; corpo menor pede menos
        self.assertEqual(vao("Resistor_THT:R_Axial_DIN0411_L9.9mm_D3.6mm_P12.70mm_Horizontal"), 5)
        self.assertEqual(vao("Resistor_THT:R_Axial_DIN0204_L3.6mm_D1.6mm_P5.08mm_Horizontal"), 3)
        # sem a medida do corpo no nome, o piso e o resistor de sempre
        self.assertEqual(vao("Diode_THT:D_DO-41_SOD81_P7.62mm_Horizontal"), 4)

    def test_peca_radial_nao_e_alargada(self):
        """Disco, eletrolitico e LED ja saem com os terminais para baixo.

        Nao ha dobra a acomodar, entao abrir o passo deles so afastaria os furos a
        toa - e um capacitor de desacoplamento longe do CI e justamente o que o
        posicionador passa o dia tentando evitar.
        """
        from perfboard.footprints import infer

        for fp in ("Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P2.50mm",
                   "Capacitor_THT:CP_Radial_D5.0mm_P2.50mm",
                   "LED_THT:LED_D5.0mm_P2.54mm"):
            d = infer(fp, ["1", "2"], "X1")
            self.assertEqual(max(x for x, _y in d.pins.values()), 1,
                             "%s foi alargado sem precisar" % fp)

    def test_passo_aberto_fica_na_nota_da_peca_nao_no_painel(self):
        """Mudar a peca em silencio nao vale - mas quinze avisos iguais tambem nao.

        Abrir o passo e o PADRAO da peca axial: acontece com todo resistor da
        placa. Como aviso, enchia o painel e escondia o que importa de verdade;
        como nota da peca, quem abre o editor dela le - e so quem se interessou.
        """
        from perfboard.footprints import infer

        d = infer("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
                  ["1", "2"], "R1")
        self.assertEqual(d.warnings, [], "isto nao e aviso, e comportamento padrao")
        self.assertIn("passo aberto", d.pin_note)
        self.assertEqual(max(x for x, _ in d.pins.values()), 4)

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
