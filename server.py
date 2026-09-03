"""Servidor local do Perfboard Router. Somente biblioteca padrao do Python.

Uso:  python server.py [--port 8765] [--no-browser]
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from perfboard import project  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
EXAMPLES = os.path.join(ROOT, "examples")
MAX_BODY = 16 * 1024 * 1024

# Buscas em andamento: id -> True quando pediram para parar. Cada resposta em fluxo
# anuncia seu id no primeiro evento, e o botao Parar chama /api/parar com ele. Usar
# id em vez de uma flag global evita que uma aba pare a busca de outra.
BUSCAS = {}
BUSCAS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "PerfboardRouter/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---------- helpers ----------

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _error(self, msg, code=400, detail=""):
        self._json({"error": msg, "detail": detail}, code)

    def _stream_solve(self, payload):
        """Responde em NDJSON: uma linha por evento de progresso, a ultima e o resultado.

        Uma busca dificil leva minutos. Sem isso o navegador fica mudo o tempo todo e
        nao da para saber se esta avancando ou travado.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")   # nginx nao segura o fluxo
        self.end_headers()

        # Se o navegador some no meio (fechou a aba, apertou Parar), a escrita falha.
        # E assim que descobrimos que ninguem esta mais escutando e paramos a busca -
        # sem isso ela continuaria queimando CPU para ninguem.
        parou = {"sim": False}
        job = "%x" % (id(payload) ^ int(time.time() * 1000))
        with BUSCAS_LOCK:
            BUSCAS[job] = False

        def escreve(obj):
            if parou["sim"]:
                return
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                parou["sim"] = True

        def pediram_parar():
            if parou["sim"]:
                return True
            with BUSCAS_LOCK:
                return BUSCAS.get(job, False)

        try:
            escreve({"tipo": "inicio", "job": job})
            resultado = project.solve(
                payload,
                progresso=lambda d: escreve({"tipo": "progresso", **d}),
                cancelado=pediram_parar)
            escreve({"tipo": "final", "resultado": resultado})
        except ValueError as exc:
            escreve({"tipo": "erro", "error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass          # o navegador desistiu; nada a fazer
        except Exception as exc:
            traceback.print_exc()
            escreve({"tipo": "erro", "error": "falha ao processar",
                     "detail": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            with BUSCAS_LOCK:
                BUSCAS.pop(job, None)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("corpo da requisicao grande demais")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_file(self, path, base):
        full = os.path.normpath(os.path.join(base, path.lstrip("/")))
        if not full.startswith(base) or not os.path.isfile(full):
            return self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # ---------- rotas ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", WEB)
        if path == "/api/examples":
            names = sorted(f for f in os.listdir(EXAMPLES) if f.endswith(".net"))
            return self._json({"examples": names})
        if path.startswith("/api/example/"):
            name = os.path.basename(path[len("/api/example/"):])
            full = os.path.join(EXAMPLES, name)
            if not os.path.isfile(full):
                return self._error("exemplo nao encontrado", 404)
            with open(full, encoding="utf-8") as fh:
                return self._json({"name": name, "netlist": fh.read()})
        return self._serve_file(path, WEB)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
        except Exception as exc:
            return self._error("JSON invalido", 400, str(exc))

        try:
            if path == "/api/analyze":
                return self._json(project.analyze(payload.get("netlist", ""),
                                                  payload.get("overrides")))
            if path == "/api/parar":
                job = str(payload.get("job") or "")
                with BUSCAS_LOCK:
                    achou = job in BUSCAS
                    if achou:
                        BUSCAS[job] = True
                return self._json({"parando": achou})
            if path == "/api/solve":
                if payload.get("stream"):
                    return self._stream_solve(payload)
                return self._json(project.solve(payload))
            return self._error("rota desconhecida: %s" % path, 404)
        except ValueError as exc:
            return self._error(str(exc), 400)
        except Exception as exc:
            traceback.print_exc()
            return self._error("falha ao processar", 500,
                               "%s: %s" % (type(exc).__name__, exc))


def main():
    ap = argparse.ArgumentParser(description="Perfboard Router - servidor local")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("Perfboard Router rodando em %s" % url)
    print("Ctrl+C para parar.")

    # Abrir os processos de busca leva ~2s. Fazer isso agora, enquanto voce ainda
    # esta escolhendo o arquivo, tira esse tempo da primeira busca - que e
    # justamente quando a espera mais incomoda.
    def aquece():
        try:
            from perfboard import paralelo
            n = paralelo.nucleos_padrao()
            if n > 1:
                paralelo._pool_de(n)
                print("busca paralela pronta: %d processos" % n)
        except Exception as exc:      # sem paralelismo o programa funciona igual
            print("busca paralela indisponivel (%s); seguindo em um processo" % exc)

    threading.Thread(target=aquece, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando.")
        srv.shutdown()


if __name__ == "__main__":
    main()
