"""Testa a aplicacao WSGI de producao: limites, erros e ajuste de carga."""
import io
import json
import os
import sys
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from deploy import wsgi  # noqa: E402

EXEMPLO = os.path.join(RAIZ, "examples", "astavel_555.net")


def chama(caminho, metodo="POST", corpo=None, ip="10.0.0.1"):
    dados = json.dumps(corpo or {}).encode("utf-8") if corpo is not None else b""
    environ = {
        "PATH_INFO": caminho,
        "REQUEST_METHOD": metodo,
        "CONTENT_LENGTH": str(len(dados)),
        "REMOTE_ADDR": ip,
        "wsgi.input": io.BytesIO(dados),
        "wsgi.errors": io.StringIO(),
    }
    capturado = {}

    def start_response(status, headers):
        capturado["status"] = int(status.split()[0])
        capturado["headers"] = dict(headers)

    saida = wsgi.application(environ, start_response)
    return capturado["status"], json.loads(b"".join(saida).decode("utf-8"))


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(EXEMPLO, encoding="utf-8") as fh:
            cls.netlist = fh.read()

    def setUp(self):
        # cada teste comeca com o balde de fichas limpo
        wsgi._balde._dados.clear()

    def test_health_nao_gasta_ficha(self):
        for _ in range(50):
            status, corpo = chama("/api/health", metodo="GET")
            self.assertEqual(status, 200)
            self.assertTrue(corpo["ok"])

    def test_analyze(self):
        status, corpo = chama("/api/analyze", corpo={"netlist": self.netlist})
        self.assertEqual(status, 200)
        self.assertEqual(corpo["summary"]["components"], 8)

    def test_solve(self):
        status, corpo = chama("/api/solve", corpo={
            "netlist": self.netlist,
            "board": {"cols": 16, "rows": 14},
            "placer": {"effort": "rapido", "seed": 1},
        })
        self.assertEqual(status, 200)
        self.assertIn("svg_top", corpo)
        self.assertIn("elapsed_s", corpo)
        self.assertEqual(corpo["stats"]["nets_failed"], 0)

    def test_netlist_invalida_da_400_sem_stack_trace(self):
        status, corpo = chama("/api/solve", corpo={"netlist": "isto nao e uma netlist"})
        self.assertEqual(status, 400)
        self.assertIn("error", corpo)
        self.assertNotIn("Traceback", json.dumps(corpo))

    def test_netlist_vazia(self):
        status, _ = chama("/api/solve", corpo={"netlist": "   "})
        self.assertEqual(status, 400)

    def test_json_quebrado(self):
        environ = {
            "PATH_INFO": "/api/solve", "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": "7", "REMOTE_ADDR": "10.0.0.9",
            "wsgi.input": io.BytesIO(b"{ nope "), "wsgi.errors": io.StringIO(),
        }
        cap = {}
        wsgi.application(environ, lambda s, h: cap.update(status=int(s.split()[0])))
        self.assertEqual(cap["status"], 400)

    def test_placa_absurda_e_recusada(self):
        status, corpo = chama("/api/solve", corpo={
            "netlist": self.netlist, "board": {"cols": 200, "rows": 200}})
        self.assertEqual(status, 413)
        self.assertIn("furos", corpo["error"])

    def test_parametros_fora_da_faixa_sao_grampeados(self):
        payload = {
            "netlist": self.netlist,
            "board": {"cols": 99999, "rows": -5, "margin_holes": 900},
            "router": {"attempts": 10 ** 6, "max_jumper": 10 ** 6, "faces": 77},
            "placer": {"seed": -3, "tries": 10 ** 6, "effort": "turbo",
                       "nucleos": 10 ** 6, "paciencia": 10 ** 6},
            "scale": 10 ** 6,
        }
        ajustado, _avisos = wsgi.valida_e_ajusta(dict(payload))
        self.assertLessEqual(ajustado["board"]["cols"], 200)
        self.assertGreaterEqual(ajustado["board"]["rows"], 4)
        self.assertLessEqual(ajustado["board"]["margin_holes"], 10)
        self.assertLessEqual(ajustado["router"]["attempts"], wsgi.MAX_ATTEMPTS)
        self.assertLessEqual(ajustado["router"]["max_jumper"], wsgi.MAX_JUMPER)
        self.assertIn(ajustado["router"]["faces"], (1, 2))
        self.assertLessEqual(ajustado["placer"]["tries"], 50)
        self.assertLessEqual(ajustado["placer"]["paciencia"], 200)
        self.assertIn(ajustado["placer"]["effort"], wsgi.PESO_ESFORCO)
        self.assertLessEqual(ajustado["scale"], 60)

    def test_nucleos_nunca_passa_do_teto(self):
        """Sem teto, um pedido manda o servidor abrir quantos processos quiser.

        E o pior tipo de buraco: nao precisa de exploit nenhum, so um numero grande
        num campo JSON. O teto sai da propria maquina (vCPU / 2, no maximo 4).
        """
        for pedido in (10 ** 6, 99, 5, -3, 0, None, "muitos"):
            ajustado, _ = wsgi.valida_e_ajusta({
                "netlist": self.netlist, "board": {"cols": 16, "rows": 14},
                "placer": {"nucleos": pedido}})
            n = ajustado["placer"]["nucleos"]
            self.assertTrue(1 <= n <= wsgi.MAX_NUCLEOS,
                            "pedindo %r saiu %r (teto %d)" % (pedido, n, wsgi.MAX_NUCLEOS))

    def test_busca_sem_limite_sobrevive_ao_servidor(self):
        """`tries: 0` quer dizer 'ate estacionar'. Quem segura e o relogio.

        Grampear isso para 1 - como o codigo fazia - desligava em producao justamente
        o recurso principal, sem ninguem perceber.
        """
        ajustado, avisos = wsgi.valida_e_ajusta({
            "netlist": self.netlist, "board": {"cols": 16, "rows": 14},
            "placer": {"tries": 0}})
        self.assertEqual(ajustado["placer"]["tries"], 0)
        self.assertTrue(any("orcamento" in a for a in avisos),
                        "o usuario precisa saber que o servidor tem prazo: %r" % avisos)
        self.assertGreater(wsgi.ORCAMENTO_BUSCA, 0)

    def test_prazo_interrompe_a_busca(self):
        """O prazo tem que valer de verdade, nao so existir como constante."""
        import time
        from perfboard.project import solve
        t0 = time.time()
        res = solve({"netlist": self.netlist, "board": {"cols": 16, "rows": 14},
                     "placer": {"effort": "rapido", "tries": 0, "paciencia": 200,
                                "nucleos": 1}},
                    cancelado=lambda: time.time() - t0 > 2)
        self.assertLess(time.time() - t0, 30, "o prazo nao interrompeu")
        self.assertIn("stats", res, "mesmo interrompido, entrega o melhor que achou")

    def test_carga_pesada_e_rebaixada_em_vez_de_recusada(self):
        """Projeto grande nao recebe erro: recebe esforco menor e um aviso."""
        grande = _netlist_sintetica(200)
        ajustado, avisos = wsgi.valida_e_ajusta({
            "netlist": grande,
            "board": {"cols": 60, "rows": 40},
            "placer": {"effort": "alto", "tries": 5},
        })
        # com tries explicito o rebaixamento continua valendo
        custo = 200 * wsgi.PESO_ESFORCO[ajustado["placer"]["effort"]] * ajustado["placer"]["tries"]
        self.assertLessEqual(custo, wsgi.ALVO_SEGUNDOS)
        self.assertTrue(avisos, "o usuario precisa saber que a carga foi reduzida")

    def test_projeto_grande_demais_e_recusado_com_clareza(self):
        status, corpo = chama("/api/solve", corpo={
            "netlist": _netlist_sintetica(wsgi.MAX_COMPONENTES + 10)})
        self.assertEqual(status, 413)
        self.assertIn("limite", corpo["error"])

    def test_limitador_de_taxa(self):
        vistos = [chama("/api/examples", metodo="GET", ip="10.9.9.9")[0]
                  for _ in range(wsgi.RAJADA + 6)]
        self.assertIn(429, vistos, "o limitador precisa entrar em algum momento")
        self.assertEqual(vistos[0], 200, "as primeiras precisam passar")

    def test_rota_desconhecida(self):
        self.assertEqual(chama("/api/nada", metodo="GET")[0], 404)
        self.assertEqual(chama("/api/solve", metodo="DELETE")[0], 405)

    def test_exemplo_nao_escapa_do_diretorio(self):
        status, _ = chama("/api/example/..%2f..%2fserver.py", metodo="GET")
        self.assertEqual(status, 404)
        status, _ = chama("/api/example/../../server.py", metodo="GET")
        self.assertEqual(status, 404)


def _netlist_sintetica(n):
    L = ['(export (version "E")', '  (design (source "x"))', "  (components"]
    for i in range(1, n + 1):
        L.append('    (comp (ref "R%d") (value "1k") '
                 '(footprint "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm") '
                 '(libsource (lib "Device") (part "R")))' % i)
    L.append("  )")
    L.append('  (libparts (libpart (lib "Device") (part "R") (pins '
             '(pin (num "1") (name "~") (type "passive")) '
             '(pin (num "2") (name "~") (type "passive")))))')
    L.append("  (nets")
    for i in range(1, n):
        L.append('    (net (code "%d") (name "N%d") (node (ref "R%d") (pin "2")) '
                 '(node (ref "R%d") (pin "1")))' % (i, i, i, i + 1))
    L.append("  ))")
    return "\n".join(L)


if __name__ == "__main__":
    unittest.main(verbosity=2)
