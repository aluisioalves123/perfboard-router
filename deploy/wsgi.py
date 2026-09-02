"""Aplicacao WSGI do Perfboard Router, para expor a API em producao.

Diferencas para o `server.py` de desenvolvimento:

* nao serve arquivo estatico - isso fica com o nginx, que faz muito melhor;
* impoe **limites de carga**, porque roteamento e caro em CPU e o endpoint e
  publico: netlist grande com esforco alto trava um worker por minutos;
* tem **limitador de taxa** por IP e **teto de concorrencia**, para uma rajada
  nao derrubar o servico;
* nunca vaza stack trace para o cliente.

Rodar com gunicorn (unica dependencia externa do projeto):

    gunicorn -c deploy/gunicorn.conf.py deploy.wsgi:application
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from perfboard import project                      # noqa: E402
from perfboard.netlist import parse_netlist        # noqa: E402

EXEMPLOS = os.path.join(RAIZ, "examples")

# ---------------------------------------------------------------- limites

MAX_CORPO = 2 * 1024 * 1024      # bytes de corpo da requisicao
MAX_COMPONENTES = 400
MAX_FUROS = 8000                 # colunas * linhas
MAX_ATTEMPTS = 20
MAX_JUMPER = 40

# Processos por requisicao. Nao e cosmetico: sem teto, um pedido com
# `placer.nucleos` alto manda o servidor abrir aquele tanto de processos.
#
# A conta que importa e `workers do gunicorn x MAX_NUCLEOS <= vCPUs da maquina`.
# Worker sincrono atende um pedido por vez, entao o numero de buscas simultaneas
# e o numero de workers - cada uma abrindo ate este tanto de processos.
MAX_NUCLEOS = max(1, min(4, (os.cpu_count() or 2) // 2))

# Tempo-alvo por requisicao. O custo cresce ~linear no numero de componentes;
# os pesos abaixo foram medidos (esforco alto: ~0,16 s por componente).
ALVO_SEGUNDOS = 20.0
PESO_ESFORCO = {"rapido": 0.02, "normal": 0.06, "alto": 0.16}
ESFORCO_DECRESCENTE = ["alto", "normal", "rapido"]

# Orcamento de tempo da busca sem limite. Local ela roda ate estacionar, o que pode
# levar minutos; num servidor compartilhado isso prenderia uma vaga e o navegador de
# quem esta na fila. Aqui ela entrega o melhor que achou quando o prazo acaba - o
# mesmo caminho do botao "Parar".
ORCAMENTO_BUSCA = float(os.environ.get("PERFBOARD_ORCAMENTO_S", 90))

# Concorrencia: roteamento e CPU-bound, entao mais que isso so piora todo mundo.
MAX_SIMULTANEOS = max(1, (os.cpu_count() or 2) - 1)
ESPERA_FILA = 2.0                # segundos esperando vaga antes de devolver 503

# Limitador de taxa por IP (balde de fichas).
TAXA_POR_MINUTO = 20
RAJADA = 8


class _Balde:
    """Balde de fichas por IP, com limpeza preguicosa."""

    def __init__(self, por_minuto: int, rajada: int):
        self.taxa = por_minuto / 60.0
        self.rajada = rajada
        self._dados = {}
        self._lock = threading.Lock()

    def permite(self, chave: str) -> bool:
        agora = time.monotonic()
        with self._lock:
            fichas, visto = self._dados.get(chave, (self.rajada, agora))
            fichas = min(self.rajada, fichas + (agora - visto) * self.taxa)
            if fichas < 1.0:
                self._dados[chave] = (fichas, agora)
                return False
            self._dados[chave] = (fichas - 1.0, agora)

            if len(self._dados) > 4096:
                velhos = [k for k, (_f, t) in self._dados.items() if agora - t > 600]
                for k in velhos:
                    del self._dados[k]
            return True


_balde = _Balde(TAXA_POR_MINUTO, RAJADA)
_vagas = threading.BoundedSemaphore(MAX_SIMULTANEOS)


class ErroCliente(Exception):
    def __init__(self, mensagem, status=400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status


# ---------------------------------------------------------------- validacao

def _inteiro(valor, minimo, maximo, padrao):
    try:
        return max(minimo, min(maximo, int(valor)))
    except (TypeError, ValueError):
        return padrao


def valida_e_ajusta(payload: dict) -> tuple:
    """Confere os limites duros e reduz a carga quando o pedido e pesado demais.

    Em vez de recusar um trabalho grande, rebaixamos esforco/tentativas ate caber
    no tempo-alvo e avisamos o que foi mexido - o usuario prefere um resultado um
    pouco pior agora a um erro seco.
    """
    avisos = []

    texto = payload.get("netlist") or ""
    if not texto.strip():
        raise ErroCliente("netlist vazia")
    if len(texto.encode("utf-8", "ignore")) > MAX_CORPO:
        raise ErroCliente("netlist grande demais (limite %d MB)" % (MAX_CORPO // (1024 * 1024)), 413)

    try:
        nl = parse_netlist(texto)
    except Exception:
        raise ErroCliente("nao consegui ler esta netlist: confira se e o .net exportado do KiCad")

    n_comp = len(nl.components)
    if n_comp == 0:
        raise ErroCliente("a netlist nao tem componentes")
    if n_comp > MAX_COMPONENTES:
        raise ErroCliente(
            "projeto com %d componentes; o limite publico e %d. "
            "Rode local para projetos maiores." % (n_comp, MAX_COMPONENTES), 413)

    placa = dict(payload.get("board") or {})
    placa["cols"] = _inteiro(placa.get("cols"), 4, 200, 24)
    placa["rows"] = _inteiro(placa.get("rows"), 4, 200, 18)
    placa["margin_holes"] = _inteiro(placa.get("margin_holes"), 0, 10, 0)
    if placa["cols"] * placa["rows"] > MAX_FUROS:
        raise ErroCliente("placa grande demais (limite %d furos)" % MAX_FUROS, 413)
    payload["board"] = placa

    roteador = dict(payload.get("router") or {})
    roteador["attempts"] = _inteiro(roteador.get("attempts"), 1, MAX_ATTEMPTS, 6)
    roteador["max_jumper"] = _inteiro(roteador.get("max_jumper"), 2, MAX_JUMPER, 14)
    roteador["faces"] = 2 if _inteiro(roteador.get("faces"), 1, 2, 1) >= 2 else 1
    payload["router"] = roteador

    posic = dict(payload.get("placer") or {})
    posic["seed"] = _inteiro(posic.get("seed"), 1, 999999, 1)
    # tries = 0 quer dizer "sem limite": aqui quem segura e o relogio, nao a contagem
    bruto_tries = posic.get("tries")
    ilimitado = bruto_tries in (0, "0", "", None)
    tries = 1 if ilimitado else _inteiro(bruto_tries, 1, 50, 1)
    posic["nucleos"] = _inteiro(posic.get("nucleos"), 1, MAX_NUCLEOS, MAX_NUCLEOS)
    posic["paciencia"] = _inteiro(posic.get("paciencia"), 2, 200, 60)
    esforco = posic.get("effort") if posic.get("effort") in PESO_ESFORCO else "normal"

    # rebaixa ate caber no tempo-alvo
    idx = ESFORCO_DECRESCENTE.index(esforco)
    while True:
        custo = n_comp * PESO_ESFORCO[ESFORCO_DECRESCENTE[idx]] * tries
        if custo <= ALVO_SEGUNDOS:
            break
        if tries > 1:
            tries -= 1
            continue
        if idx < len(ESFORCO_DECRESCENTE) - 1:
            idx += 1
            continue
        break

    novo_esforco = ESFORCO_DECRESCENTE[idx]
    if novo_esforco != esforco:
        avisos.append("esforco reduzido de '%s' para '%s' (projeto com %d componentes)"
                      % (esforco, novo_esforco, n_comp))
    if not ilimitado and tries != _inteiro(bruto_tries, 1, 50, 1):
        avisos.append("tentativas reduzidas para %d para caber no tempo do servidor" % tries)

    posic["effort"] = novo_esforco
    posic["tries"] = 0 if ilimitado else tries
    if ilimitado:
        avisos.append("busca com orcamento de %ds neste servidor; rodando local ela vai "
                      "ate estacionar sozinha" % int(ORCAMENTO_BUSCA))
    payload["placer"] = posic
    payload["scale"] = _inteiro(payload.get("scale"), 8, 60, 30)

    return payload, avisos


# ---------------------------------------------------------------- rotas

def rota_analyze(payload):
    texto = payload.get("netlist") or ""
    if not texto.strip():
        raise ErroCliente("netlist vazia")
    if len(texto.encode("utf-8", "ignore")) > MAX_CORPO:
        raise ErroCliente("netlist grande demais", 413)
    try:
        return project.analyze(texto, payload.get("overrides"))
    except ErroCliente:
        raise
    except Exception:
        raise ErroCliente("nao consegui ler esta netlist: confira se e o .net exportado do KiCad")


def stream_solve(payload, start_response):
    """Mesma coisa do servidor de desenvolvimento, mas como gerador WSGI.

    O nginx desta instalacao ja vem com `proxy_buffering off`, entao os eventos
    chegam ao navegador na hora em vez de so no fim.
    """
    payload, avisos = valida_e_ajusta(payload)
    start_response("200 OK", [
        ("Content-Type", "application/x-ndjson; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("X-Accel-Buffering", "no"),
    ])

    def linha(obj):
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

    def gerar():
        # O solve roda numa thread e empurra eventos para a fila; o gerador consome e
        # entrega na hora. Acumular numa lista para mandar tudo no fim - como estava
        # antes - anularia o proposito de transmitir progresso.
        yield linha({"tipo": "inicio", "server_notes": avisos})

        if not _vagas.acquire(timeout=ESPERA_FILA):
            yield linha({"tipo": "erro", "error": "servidor ocupado, tente de novo"})
            return

        fila = queue.Queue()
        FIM = object()

        prazo = time.monotonic() + ORCAMENTO_BUSCA

        def trabalha():
            inicio = time.monotonic()
            try:
                resultado = project.solve(
                    payload, progresso=lambda d: fila.put({"tipo": "progresso", **d}),
                    cancelado=lambda: time.monotonic() > prazo)
                resultado["server_notes"] = avisos
                resultado["elapsed_s"] = round(time.monotonic() - inicio, 2)
                fila.put({"tipo": "final", "resultado": resultado})
            except ErroCliente as exc:
                fila.put({"tipo": "erro", "error": exc.mensagem})
            except Exception:
                traceback.print_exc()
                fila.put({"tipo": "erro", "error": "falha ao processar"})
            finally:
                fila.put(FIM)

        t = threading.Thread(target=trabalha, daemon=True)
        t.start()
        try:
            while True:
                item = fila.get()
                if item is FIM:
                    break
                yield linha(item)
        finally:
            _vagas.release()

    return gerar()


def rota_solve(payload):
    payload, avisos = valida_e_ajusta(payload)
    if not _vagas.acquire(timeout=ESPERA_FILA):
        raise ErroCliente("servidor ocupado, tente de novo em alguns segundos", 503)
    try:
        inicio = time.monotonic()
        prazo = inicio + ORCAMENTO_BUSCA
        resultado = project.solve(payload, cancelado=lambda: time.monotonic() > prazo)
    finally:
        _vagas.release()
    resultado["server_notes"] = avisos
    resultado["elapsed_s"] = round(time.monotonic() - inicio, 2)
    return resultado


def rota_exemplos():
    try:
        nomes = sorted(f for f in os.listdir(EXEMPLOS) if f.endswith(".net"))
    except OSError:
        nomes = []
    return {"examples": nomes}


def rota_exemplo(nome):
    nome = os.path.basename(nome)
    caminho = os.path.join(EXEMPLOS, nome)
    if not nome.endswith(".net") or not os.path.isfile(caminho):
        raise ErroCliente("exemplo nao encontrado", 404)
    with open(caminho, encoding="utf-8") as fh:
        return {"name": nome, "netlist": fh.read()}


# ---------------------------------------------------------------- WSGI

STATUS = {200: "200 OK", 400: "400 Bad Request", 404: "404 Not Found",
          405: "405 Method Not Allowed", 413: "413 Payload Too Large",
          429: "429 Too Many Requests", 500: "500 Internal Server Error",
          503: "503 Service Unavailable"}


def _ip(environ):
    # confie no X-Forwarded-For apenas porque o nginx a frente o reescreve;
    # exposto direto na internet, use REMOTE_ADDR.
    encaminhado = environ.get("HTTP_X_FORWARDED_FOR", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "?")


def _responde(start_response, status, corpo):
    dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    cabecalhos = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(dados))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if status == 503:
        cabecalhos.append(("Retry-After", "5"))
    if status == 429:
        cabecalhos.append(("Retry-After", "10"))
    start_response(STATUS.get(status, "500 Internal Server Error"), cabecalhos)
    return [dados]


def application(environ, start_response):
    caminho = environ.get("PATH_INFO", "")
    metodo = environ.get("REQUEST_METHOD", "GET")

    try:
        if caminho == "/api/health":
            return _responde(start_response, 200, {"ok": True, "vagas": MAX_SIMULTANEOS})

        if not _balde.permite(_ip(environ)):
            raise ErroCliente("muitas requisicoes; espere alguns segundos", 429)

        if metodo == "GET":
            if caminho == "/api/examples":
                return _responde(start_response, 200, rota_exemplos())
            if caminho.startswith("/api/example/"):
                return _responde(start_response, 200,
                                 rota_exemplo(caminho[len("/api/example/"):]))
            raise ErroCliente("rota desconhecida", 404)

        if metodo != "POST":
            raise ErroCliente("metodo nao suportado", 405)

        try:
            tamanho = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            tamanho = 0
        if tamanho > MAX_CORPO:
            raise ErroCliente("corpo grande demais", 413)
        bruto = environ["wsgi.input"].read(tamanho) if tamanho > 0 else b""
        try:
            payload = json.loads(bruto.decode("utf-8")) if bruto else {}
        except (ValueError, UnicodeDecodeError):
            raise ErroCliente("JSON invalido")
        if not isinstance(payload, dict):
            raise ErroCliente("JSON invalido")

        if caminho == "/api/analyze":
            return _responde(start_response, 200, rota_analyze(payload))
        if caminho == "/api/solve":
            if payload.get("stream"):
                return stream_solve(payload, start_response)
            return _responde(start_response, 200, rota_solve(payload))
        raise ErroCliente("rota desconhecida", 404)

    except ErroCliente as exc:
        return _responde(start_response, exc.status, {"error": exc.mensagem})
    except Exception:
        # detalhe vai para o log do servidor, nunca para o cliente
        traceback.print_exc(file=environ.get("wsgi.errors", sys.stderr))
        return _responde(start_response, 500, {"error": "falha interna ao processar"})
