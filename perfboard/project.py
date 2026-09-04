"""Orquestracao: netlist -> footprints -> posicionamento -> roteamento -> desenho."""
from __future__ import annotations

import math
import time

from . import bancada
from . import footprints as fpmod
from . import manual
from . import paralelo
from . import render
from .board import BoardSpec, Layout, Placement, PITCH_MM, rotulador
from .netlist import parse_netlist
from .placer import auto_place
from .router import Router, RouterConfig


def analyze(netlist_text: str, overrides=None) -> dict:
    """Le a netlist e devolve componentes, footprints deduzidos e avisos."""
    nl = parse_netlist(netlist_text)
    lib = fpmod.build_library(nl, overrides)

    comps = []
    warnings = []
    for ref in sorted(nl.components):
        c = nl.components[ref]
        fp = lib[ref]
        comps.append({
            "ref": ref,
            "value": c.value,
            "footprint": c.footprint,
            "pins": c.pins,
            "pattern": fp.to_json(),
        })
        for w in fp.warnings:
            warnings.append("%s: %s" % (ref, w))

    nets = [{"name": n.name, "code": n.code,
             "nodes": ["%s.%s" % (r, p) for r, p in n.nodes]}
            for n in nl.nets]

    return {
        "summary": nl.summary(),
        "components": comps,
        "nets": nets,
        "warnings": warnings,
        "decoupling": sugere_desacoplamento(nl),
        "suggested_board": suggest_board(lib, nl),
    }


GND_HINTS = ("GND", "VSS", "AGND", "DGND", "0V")
PWR_HINTS = ("VCC", "VDD", "VEE", "+5V", "+3V3", "+3.3V", "+12V", "-12V", "+15V",
             "-15V", "VBUS", "VIN", "VS", "AVCC", "AVDD", "5V", "3V3", "12V")


def _e_terra(nome: str) -> bool:
    n = (nome or "").upper()
    return any(k in n for k in GND_HINTS)


def _e_alimentacao(nome: str) -> bool:
    n = (nome or "").upper()
    if _e_terra(n):
        return False
    return any(k in n for k in PWR_HINTS)


def sugere_desacoplamento(nl) -> list:
    """Encontra pares capacitor/CI que parecem ser desacoplamento.

    Criterio: um capacitor de 2 pinos cujas duas redes sao alimentacao e terra, e
    que compartilha essas duas redes com um mesmo CI. E isso que um capacitor de
    desacoplamento e - o que a netlist NAO diz e que ele precisa ficar colado no
    pino de alimentacao, porque o que importa e a area do laco.
    """
    rede_de = {}
    for net in nl.nets:
        for no in net.nodes:
            rede_de[no] = net.name

    # pinos de cada rede, por componente
    pinos_por_rede = {}
    for net in nl.nets:
        for ref, pin in net.nodes:
            pinos_por_rede.setdefault(net.name, []).append((ref, pin))

    cis = [ref for ref, c in nl.components.items() if len(c.pins) >= 5]
    pares = []

    for ref, comp in nl.components.items():
        if len(comp.pins) != 2:
            continue
        if not (ref[:1].upper() == "C" or "apacitor" in (comp.footprint or "")):
            continue

        redes = [rede_de.get((ref, pin)) for pin in comp.pins]
        if None in redes:
            continue
        pino_pwr = pino_gnd = None
        for pin, rede in zip(comp.pins, redes):
            if _e_alimentacao(rede):
                pino_pwr = (pin, rede)
            elif _e_terra(rede):
                pino_gnd = (pin, rede)
        if not pino_pwr or not pino_gnd:
            continue

        for ci in cis:
            ci_pwr = [p for r, p in pinos_por_rede.get(pino_pwr[1], []) if r == ci]
            ci_gnd = [p for r, p in pinos_por_rede.get(pino_gnd[1], []) if r == ci]
            if not ci_pwr or not ci_gnd:
                continue
            pares.append({
                "cap": ref,
                "ic": ci,
                "cap_pin_pwr": pino_pwr[0], "ic_pin_pwr": ci_pwr[0], "net_pwr": pino_pwr[1],
                "cap_pin_gnd": pino_gnd[0], "ic_pin_gnd": ci_gnd[0], "net_gnd": pino_gnd[1],
                "auto": True,
            })
            break   # um capacitor desacopla um CI

    return pares


def suggest_board(lib: dict, nl) -> dict:
    """Chute inicial de tamanho de placa: area dos CORPOS com folga.

    Usa o corpo, nao o vao dos pinos: um borne ocupa bem mais espaco do que a
    distancia entre os terminais dele.
    """
    area = 0
    max_w = max_h = 1
    for fp in lib.values():
        w, h = fp.body_size
        area += w * h
        max_w = max(max_w, w)
        max_h = max(max_h, h)
    pins = sum(len(fp.pins) for fp in lib.values())
    need = max(area * 3 + pins * 2, 48)
    side = int(math.ceil(math.sqrt(need)))
    cols = max(side, max_w + 2, 10)
    rows = max(int(math.ceil(need / cols)), max_h + 2, 8)
    return {"cols": cols, "rows": rows,
            "width_mm": round((cols - 1) * PITCH_MM, 1),
            "height_mm": round((rows - 1) * PITCH_MM, 1)}


def rotea_com_limite(layout, nl, rcfg, limite):
    """Roteia respeitando um TETO de jumpers, calibrando o custo sozinho.

    Pedir "no maximo 6 jumpers" e uma decisao que da para tomar olhando a placa;
    escolher "custo 110 por jumper" nao e. Como o numero de jumpers cai de forma
    monotona conforme o custo sobe, subimos o custo ate caber no teto.

    Devolve (resultado, relato). Se nem com o custo la em cima der para respeitar o
    teto sem deixar pino solto, devolvemos o melhor resultado VALIDO e dizemos no
    relato qual foi o minimo alcancavel - nunca entregamos layout quebrado so para
    cumprir o numero.
    """
    from copy import copy

    if limite is None:
        return Router(layout, nl, rcfg).route(), None

    if limite <= 0:
        cfg = copy(rcfg)
        cfg.allow_jumpers = False
        res = Router(layout, nl, cfg).route()
        relato = None
        if res["stats"]["orphan_pins"]:
            relato = {"limite": 0, "obtido": 0, "atingido": False,
                      "mensagem": "com zero jumpers sobraram %d pino(s) sem ligacao"
                                  % len(res["stats"]["orphan_pins"])}
        return res, relato

    # A busca e so para DESCOBRIR o custo certo, entao roda com menos rodadas de
    # rip-up: jumper caro deixa o roteamento dificil, o PathFinder para de convergir
    # e cada passo cairia na legalizacao estrita inteira. No fim refazemos uma unica
    # vez com o esforco cheio, no custo encontrado.
    sonda = copy(rcfg)
    sonda.attempts = max(2, min(rcfg.attempts, 3))

    cfg = copy(sonda)
    melhor = None          # melhor resultado sem pino solto
    tentativas = []
    sem_progresso = 0

    def refina(base_custo):
        """Refaz com o esforco cheio no custo encontrado."""
        final = copy(rcfg)
        final.jumper_base = base_custo
        return Router(layout, nl, final).route()

    for passo in range(3):
        res = Router(layout, nl, cfg).route()
        n = res["stats"]["jumpers"]
        valido = not res["stats"]["orphan_pins"]
        tentativas.append((cfg.jumper_base, n, valido))

        # entre os validos, menos jumpers; empatou, o que der menos trabalho manual
        if valido:
            chave = (n, res["stats"]["quinas"] + res["stats"]["corridas"])
            if melhor is None or chave < melhor[2]:
                melhor = (res, cfg.jumper_base, chave)
        if valido and n <= limite:
            if passo:
                cheio = refina(cfg.jumper_base)
                if not cheio["stats"]["orphan_pins"] and cheio["stats"]["jumpers"] <= limite:
                    res = cheio
                return res, {"limite": limite, "obtido": res["stats"]["jumpers"],
                             "atingido": True,
                             "mensagem": "cheguei a %d jumper(s) encarecendo o jumper "
                                         "ate %.0f" % (res["stats"]["jumpers"], cfg.jumper_base)}
            return res, None

        # encarecer mais nao esta ajudando: para de gastar tempo
        if len(tentativas) >= 2 and n >= tentativas[-2][1]:
            sem_progresso += 1
            if sem_progresso >= 2:
                break
        else:
            sem_progresso = 0

        # sobrou jumper: encarece na proporcao do excesso, com piso de 1.5x
        fator = 1.6 if n <= limite else min(2.2, max(1.5, 1.0 + (n - limite) / max(1, limite)))
        cfg.jumper_base = min(cfg.jumper_base * fator, rcfg.jumper_base * 12.0)

    if melhor is None:
        res = Router(layout, nl, rcfg).route()
        return res, {"limite": limite, "obtido": res["stats"]["jumpers"], "atingido": False,
                     "mensagem": "nao consegui uma solucao valida dentro do limite"}

    # Teto inalcancavel: nao vale gastar mais uma passada cheia para confirmar algo
    # que ja sabemos que nao fecha. Devolvemos o melhor valido que a sonda achou.
    res, base, _chave = melhor
    obtido = res["stats"]["jumpers"]
    return res, {
        "limite": limite, "obtido": obtido, "atingido": obtido <= limite,
        "mensagem": ("nao deu para ficar em %d jumper(s) sem deixar pino solto; "
                     "o minimo que fechou tudo foi %d" % (limite, obtido))
                    if obtido > limite else None,
    }


def build_layout(nl, spec: BoardSpec, lib: dict, placements_json=None) -> Layout:
    placements = {}
    for d in (placements_json or []):
        if d.get("ref") in lib:
            placements[d["ref"]] = Placement.from_json(d)
    for ref in lib:
        placements.setdefault(ref, Placement(ref=ref))
    return Layout(spec, lib, placements)


PASSO_MM = 2.54          # distancia entre furos de uma perfboard de 0,1"

# Quanto o peso de uma rede sobe a cada tentativa em que ela nao fecha, e ate onde.
# O teto existe para a busca nao virar obsessao por uma rede so, esquecendo o resto.
PASSO_PESO_REDE = 0.4
TETO_PESO_REDE = 6.0


def esforco_de_bancada(st, cfg) -> float:
    """Quanto trabalho manual esta solucao custa, num numero so.

    Usa os MESMOS pesos que o roteador recebeu: quem escolheu o perfil
    "menos quinas" quer que a busca tambem prefira menos quinas. Assim o criterio
    de escolher entre duas solucoes 100% ligadas e o mesmo criterio que guiou cada
    trilha - nao adianta rotear para um objetivo e ranquear por outro.
    """
    return (cfg.turn_cost * st["quinas"]
            + cfg.via_cost * st["vias"]
            + cfg.jumper_base * st["jumpers"]
            + cfg.trace_cost * (st["total_mm"] / PASSO_MM))


def solve(payload: dict, progresso=None, cancelado=None) -> dict:
    text = payload.get("netlist") or ""
    if not text.strip():
        raise ValueError("netlist vazia")

    nl = parse_netlist(text)
    overrides = payload.get("overrides") or {}
    lib = fpmod.build_library(nl, overrides)
    spec = BoardSpec.from_json(payload.get("board") or {})
    rcfg = RouterConfig.from_json(payload.get("router") or {})
    rjson = payload.get("router") or {}
    limite_jumpers = rjson.get("max_jumpers")
    if limite_jumpers is not None and limite_jumpers != "":
        limite_jumpers = max(0, min(200, int(limite_jumpers)))
    pcfg = payload.get("placer") or {}
    auto = payload.get("auto_place", True)

    # Cada tentativa e um sorteio independente: semente diferente e ordem de
    # avaliacao diferente levam a trajetorias completamente distintas. Nao ha numero
    # "certo" de tentativas - quem decide quando esta bom e o usuario. Por isso
    # `tries` pode vir vazio/zero, e ai roda ate fechar 100% ou ate mandarem parar.
    bruto = pcfg.get("tries", 1) if auto else 1
    try:
        tries = int(bruto)
    except (TypeError, ValueError):
        tries = 0
    infinito = auto and tries <= 0

    # Achar a primeira solucao 100% ligada e so metade do trabalho: entre varias
    # solucoes completas uma da bem menos trabalho de montar que outra. Por isso,
    # depois da primeira, a busca continua melhorando ate estacionar - e o plato,
    # que o usuario ve no grafico. `paciencia` e quantas tentativas seguidas sem
    # melhora bastam para declarar que estacionou.
    modo = str(pcfg.get("modo", "otimizar"))
    # Paciencia conta SOLUCOES COMPLETAS sem melhora, nao tentativas: uma tentativa
    # que nem fecha nao diz nada sobre a qualidade ter estacionado. Num caso real so
    # ~5% das tentativas fecham, entao contar tentativas cruas pararia cedo demais.
    paciencia = max(1, int(pcfg.get("paciencia", 60) or 60))
    # E paciencia fixa tambem engana: neste circuito as melhoras vieram na 5a, 22a,
    # 111a e 168a tentativa - quem desistisse depois do primeiro silencio longo
    # perderia as duas ultimas, que juntas tiraram 15 quinas e 5 vias. Entao o limite
    # tambem acompanha o maior silencio ja vencido: se voce ja aguentou 6 solucoes
    # sem ganho e a 7a melhorou, da para aguentar de novo.
    fator_silencio = float(pcfg.get("fator_silencio", 1.5) or 1.5)
    if not auto:
        tries = 1
    elif not infinito:
        tries = max(1, tries)
    seed0 = int(pcfg.get("seed", 1))

    def desistir():
        return bool(cancelado and cancelado())

    # pares de desacoplamento: por padrao usa os detectados, mas quem manda e a UI
    if "decoupling" in payload:
        pares = payload.get("decoupling") or []
    else:
        pares = sugere_desacoplamento(nl)

    def avisa(**dados):
        if progresso is not None:
            try:
                progresso(dados)
            except Exception:
                pass

    best = None
    melhor_soltos = None
    melhor_esforco = None
    sem_melhora = 0
    maior_silencio = 0
    fechadas = 0
    primeira_fechada = None
    historico = []
    plato = False
    t_zero = time.time()
    # quantos pinos o circuito tem ao todo: e a base do "% ja ligado" no grafico
    total_pinos = max(1, nl.summary()["pins"])
    # Rede que volta com pino solto ganha peso; o posicionador aperta ela na
    # tentativa seguinte. Sem isto, cada sorteio recomeca ignorando tudo o que os
    # anteriores descobriram - e o mesmo emaranhado se repete.
    pesos_de_rede = {}
    esforco_base = None      # esforco da PRIMEIRA solucao completa; a economia parte dela
    primeira_esforco = None  # e como ela era, para a tela mostrar o antes x depois
    i = -1

    # De onde vem cada tentativa. Uma tentativa so - arrastar uma peca e reroteiar -
    # roda aqui mesmo: abrir processo custaria mais que o trabalho. A busca longa vai
    # para o bando, porque as tentativas sao independentes e a maquina tem varios
    # nucleos parados esperando.
    nucleos = int(pcfg.get("nucleos", 0) or 0)
    if nucleos <= 0:
        nucleos = paralelo.nucleos_padrao() if (auto and infinito) else 1

    def _sequencial():
        """Tentativas uma a uma, no proprio processo - com progresso detalhado."""
        n = seed0
        while True:
            avisa(fase="posicionando", tentativa=i + 2,
                  de_tentativas=0 if infinito else tries, melhor=melhor_soltos)
            lay = build_layout(nl, spec, lib, payload.get("placements"))
            rep = None
            if auto:
                rep = auto_place(
                    lay, nl, seed=n,
                    effort=pcfg.get("effort", "alto"),
                    keep_existing=bool(pcfg.get("keep_existing", False)),
                    edge_pull=float(pcfg.get("edge_pull", 0.6)),
                    decoupling=pares,
                    pesos_de_rede=dict(pesos_de_rede),
                )
            r = Router(lay, nl, rcfg)

            def _repassa(d, _i=i + 1):
                # o router manda suas proprias chaves; as nossas so completam o contexto
                dados = {"tentativa": _i + 1,
                         "de_tentativas": 0 if infinito else tries,
                         "melhor": melhor_soltos}
                dados.update(d)
                avisa(**dados)

            r.progresso = _repassa if progresso is not None else None
            # Roteamos UMA vez por candidato, com o custo padrao. A calibracao do teto
            # de jumpers e cara (varias passadas) e so faz sentido no vencedor - rodar
            # por candidato multiplicava o tempo por 6 sem melhorar a escolha.
            yield {"semente": n,
                   "placements": [pl.to_json() for pl in lay.placements.values()],
                   "resultado": r.route(), "relatorio": rep, "problemas": lay.problems()}
            n += 1

    bando = None
    if nucleos > 1:
        bando = paralelo.Bando(text, payload.get("board") or {}, overrides, rjson,
                               pcfg, payload.get("placements"), pares, nucleos=nucleos)
        # avisa ANTES de abrir: subir os processos leva alguns segundos e sem isto a
        # tela fica muda justo na largada, parecendo travada
        avisa(fase="bando", nucleos=bando.nucleos, fase_detalhe="abrindo")
        bando.__enter__()
        avisa(fase="bando", nucleos=bando.nucleos, fase_detalhe="pronto")

    try:
        fonte = bando.resultados(seed0) if bando is not None else _sequencial()
        for cand in fonte:
            i += 1
            if not infinito and i >= tries:
                break
            if i and desistir():
                avisa(fase="interrompido", tentativa=i,
                      de_tentativas=0 if infinito else tries, melhor=melhor_soltos)
                break

            cand_result = cand["resultado"]
            problemas = cand["problemas"]

            # Aprende com esta tentativa: rede que nao fechou fica mais cara de
            # espalhar na proxima. Teto para nao virar obsessao por uma rede so.
            for rota in cand_result["routes"]:
                if rota.get("orphans") or not rota.get("ok", True):
                    atual = pesos_de_rede.get(rota["name"], 1.0)
                    pesos_de_rede[rota["name"]] = min(TETO_PESO_REDE,
                                                      atual + PASSO_PESO_REDE)
            if bando is not None:
                bando.pesos(pesos_de_rede)
            st = cand_result["stats"]
            soltos_aqui = len(st.get("orphan_pins", []))
            esforco = esforco_de_bancada(st, rcfg)
            # Primeiro completude (sem isso a placa nao serve), depois trabalho de bancada.
            score = (soltos_aqui, st["nets_failed"],
                     len(cand_result["shorts"]), len(problemas),
                     max(0, st["jumpers"] - limite_jumpers) if limite_jumpers is not None else 0,
                     esforco, st["total_mm"])
            fechou = (soltos_aqui == 0 and not cand_result["shorts"] and not problemas)

            if melhor_soltos is None or soltos_aqui < melhor_soltos:
                melhor_soltos = soltos_aqui
            melhorou = best is None or score < best[0]
            if melhorou:
                best = (score, cand["placements"], cand_result,
                        cand["relatorio"], cand["semente"])

            if fechou:
                fechadas += 1
                if melhorou or primeira_fechada is None:
                    maior_silencio = max(maior_silencio, sem_melhora)
                    sem_melhora = 0
                else:
                    sem_melhora += 1
                if primeira_fechada is None:
                    primeira_fechada = {"tentativa": i + 1,
                                        "segundos": round(time.time() - t_zero, 1)}
                if esforco_base is None:
                    esforco_base = esforco
                    primeira_esforco = {
                        "esforco": round(esforco, 1), "quinas": st["quinas"],
                        "vias": st["vias"], "jumpers": st["jumpers"],
                        "mm": st["total_mm"]}
                if melhorou:
                    melhor_esforco = esforco
            limite_silencio = max(paciencia, int(maior_silencio * fator_silencio + 0.999))

            # Uma linha por tentativa: e com isto que a tela desenha a curva e mostra
            # onde a melhora estacionou.
            ponto = {"tentativa": i + 1, "segundos": round(time.time() - t_zero, 2),
                     "soltos": soltos_aqui, "fechou": fechou,
                     "esforco": round(esforco, 1) if fechou else None,
                     "quinas": st["quinas"], "vias": st["vias"],
                     "jumpers": st["jumpers"], "mm": st["total_mm"],
                     "melhorou": bool(melhorou and fechou),
                     "melhor_esforco": round(melhor_esforco, 1) if melhor_esforco else None,
                     # o que a tela desenha: sempre uma porcentagem que sobe
                     "ligado": round(100.0 * (total_pinos - soltos_aqui) / total_pinos, 2),
                     "economia": (round(100.0 * (esforco_base - melhor_esforco) / esforco_base, 2)
                                  if esforco_base else None)}
            historico.append(ponto)
            avisa(fase="tentativa_concluida", tentativa=i + 1,
                  de_tentativas=0 if infinito else tries,
                  soltos=soltos_aqui, melhor=melhor_soltos, ponto=ponto,
                  fechadas=fechadas, sem_melhora=sem_melhora, paciencia=limite_silencio)

            if fechou:
                if modo == "primeiro":
                    break
                if not infinito and i + 1 >= tries:
                    break
                if sem_melhora >= limite_silencio:
                    # estacionou: mais tentativas so gastam tempo
                    plato = True
                    avisa(fase="plato", tentativa=i + 1, sem_melhora=sem_melhora,
                          fechadas=fechadas, paciencia=limite_silencio,
                          esforco=round(melhor_esforco, 1) if melhor_esforco else None)
                    break
    finally:
        if bando is not None:
            bando.__exit__(None, None, None)

    _, melhores_pos, result, place_report, used_seed = best
    layout = build_layout(nl, spec, lib, melhores_pos)
    router = Router(layout, nl, rcfg)     # so para nomear redes; nao roteia de novo

    # agora sim, so no posicionamento escolhido, calibra o custo ate caber no teto
    relato_jumpers = None
    if limite_jumpers is not None and result["stats"]["jumpers"] > limite_jumpers:
        avisa(fase="calibrando_jumpers", limite=limite_jumpers,
              atual=result["stats"]["jumpers"])
        result, relato_jumpers = rotea_com_limite(layout, nl, rcfg, limite_jumpers)
    elif limite_jumpers == 0:
        result, relato_jumpers = rotea_com_limite(layout, nl, rcfg, 0)
    if place_report is not None:
        place_report["seed"] = used_seed
        place_report["tries"] = i + 1
        place_report["ilimitado"] = infinito
        place_report["interrompido"] = bool(infinito and desistir())
        place_report["modo"] = modo
        place_report["plato"] = plato
        place_report["paciencia"] = paciencia
        place_report["limite_silencio"] = max(
            paciencia, int(maior_silencio * fator_silencio + 0.999))
        place_report["maior_silencio"] = maior_silencio
        place_report["fechadas"] = fechadas
        place_report["nucleos"] = nucleos
        # o que a busca esta chamando de "melhor", em numeros, para a tela poder dizer
        place_report["objetivo"] = {
            "quina": rcfg.turn_cost, "via": rcfg.via_cost,
            "jumper": rcfg.jumper_base, "furo_de_trilha": rcfg.trace_cost,
            "primeira": primeira_esforco, "melhor": (round(melhor_esforco, 1)
                                                     if melhor_esforco else None),
        }
        place_report["sem_melhora"] = sem_melhora
        place_report["primeira_fechada"] = primeira_fechada
        place_report["segundos"] = round(time.time() - t_zero, 1)

    scale = float(payload.get("scale", 26))
    style = payload.get("label_style", "letra")
    # Um rotulador so para toda a resposta: o nome do furo tem que sair igual no
    # desenho, na verificacao e no guia de montagem - senao a pessoa procura na
    # placa um furo que o texto chama de outro jeito.
    nome_do_furo = rotulador(spec, style)
    hole_nets = result["occupancy"]["holes"]
    # Um plano so, usado pelo desenho E pelo guia: assim nao ha como um dizer 13
    # juntas e o outro 14.
    pinos_da_placa = {}
    for ref in layout.placements:
        for pino, cel in layout.pin_holes(ref).items():
            pinos_da_placa[tuple(cel)] = (ref, pino)
    plano_bancada = bancada.plano_de_montagem(result["routes"], nome_do_furo,
                                              pinos_da_placa)
    # O programa confere a propria saida contra as regras da bancada. Achar isso a
    # olho, no desenho, e trabalho de quem monta - e ele ja tem trabalho demais.
    problemas_de_montagem = bancada.confere(plano_bancada, pinos_da_placa,
                                            result["routes"])
    svg_top = render.render_board(layout, result["routes"], "top", scale, hole_nets,
                                  label_style=style, plano=plano_bancada)
    svg_bottom = render.render_board(layout, result["routes"], "bottom", scale, hole_nets,
                                     label_style=style, plano=plano_bancada)

    fp_warnings = ["%s: %s" % (ref, w) for ref in sorted(lib) for w in lib[ref].warnings]

    # Sem jumper, o roteamento tem que ser planar - e a maioria dos circuitos com CI
    # nao e. Se sobrou pino solto, descobrimos quantos jumpers resolveriam, na MESMA
    # posicao das pecas, para o usuario decidir com numero na mao.
    suggestion = None
    soltos = len(result["stats"]["orphan_pins"])
    if soltos and payload.get("explain", True):
        avisa(fase="diagnosticando", soltos=soltos)
        def tentar(**mudancas):
            alt_cfg = RouterConfig.from_json(payload.get("router") or {})
            for k, v in mudancas.items():
                setattr(alt_cfg, k, v)
            return Router(layout, nl, alt_cfg).route()

        # 1) duas faces resolveria? (so faz sentido perguntar se ele esta em face unica)
        if not rcfg.two_sided:
            # A segunda face so ajuda se a trilha do lado dos componentes for
            # permitida - e ela e o que o usuario normalmente NAO quer, porque corre
            # entre os corpos das pecas. Entao a sugestao precisa dizer isso junto,
            # senao manda trocar de placa para nada.
            alt = tentar(faces=2, allow_jumpers=False, trilha_em_cima=True)
            if not alt["stats"]["orphan_pins"]:
                suggestion = {
                    "kind": "duas_faces",
                    "vias": alt["stats"]["vias"],
                    "message": ("%d pino(s) ficam soltos nesta placa de face unica. Com "
                                "PERFBOARD DE 2 FACES e trilha no lado dos componentes "
                                "liberada, o circuito fecha 100%%, com %d via(s) e nenhum "
                                "jumper. Repare que essa trilha corre entre os corpos das "
                                "pecas: so vale se voce conseguir soldar ali."
                                % (soltos, alt["stats"]["vias"])),
                }

        # 2) e com jumpers, quantos bastariam?
        if suggestion is None and not rcfg.allow_jumpers:
            alt = tentar(allow_jumpers=True, jumper_base=max(rcfg.jumper_base, 400.0))
            if not alt["stats"]["orphan_pins"]:
                suggestion = {
                    "kind": "jumpers_minimos",
                    "jumpers": alt["stats"]["jumpers"],
                    "message": ("Sem jumper nenhum, %d pino(s) ficam soltos nesta posicao das pecas. "
                                "Com apenas %d jumper(s) o circuito fecha 100%%."
                                % (soltos, alt["stats"]["jumpers"])),
                }

        if suggestion is None:
            suggestion = {
                "kind": "sem_saida",
                "message": ("Esta posicao das pecas nao fecha nem afrouxando as regras. Aumente "
                            "'Tentativas' (ele testa outras posicoes e fica com a melhor), use uma "
                            "placa maior, ou trave as pecas criticas e mova o resto a mao."),
            }

    for o in result["stats"].get("orphan_pins", []):
        o["label"] = nome_do_furo(o["cell"][0], o["cell"][1])
    for route in result["routes"]:
        for o in route.get("orphans", []):
            o["label"] = nome_do_furo(o["cell"][0], o["cell"][1])

    # quao perto os capacitores de desacoplamento realmente ficaram
    relatorio_desacopla = []
    for d in pares:
        item = dict(d)
        for rotulo, pa, pb in (("pwr", d.get("cap_pin_pwr"), d.get("ic_pin_pwr")),
                               ("gnd", d.get("cap_pin_gnd"), d.get("ic_pin_gnd"))):
            ca = layout.pin_holes(d.get("cap", "")).get(str(pa))
            cb = layout.pin_holes(d.get("ic", "")).get(str(pb))
            if ca and cb:
                furos = abs(ca[0] - cb[0]) + abs(ca[1] - cb[1])
                item[rotulo + "_furos"] = furos
                item[rotulo + "_mm"] = round(furos * PITCH_MM, 1)
                item[rotulo + "_de"] = nome_do_furo(ca[0], ca[1])
                item[rotulo + "_ate"] = nome_do_furo(cb[0], cb[1])
        # O que importa e a AREA DO LACO: alimentacao -> capacitor -> terra -> CI.
        # Ele tem um piso fisico: se os pinos de alimentacao e terra do CI estao
        # longe um do outro (num DIP-16 o VCC e o GND ficam em cantos opostos, 10
        # furos), nenhum posicionamento fecha o laco abaixo dessa separacao menos o
        # vao do proprio capacitor. E o capacitor nao pode ser posto sobre o CI.
        laco = item.get("pwr_furos", 0) + item.get("gnd_furos", 0)
        pinos_ci = layout.pin_holes(d.get("ic", ""))
        pinos_cap = layout.pin_holes(d.get("cap", ""))
        a = pinos_ci.get(str(d.get("ic_pin_pwr")))
        b = pinos_ci.get(str(d.get("ic_pin_gnd")))
        ca = pinos_cap.get(str(d.get("cap_pin_pwr")))
        cb = pinos_cap.get(str(d.get("cap_pin_gnd")))
        sep = abs(a[0] - b[0]) + abs(a[1] - b[1]) if a and b else 0
        vao = abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) if ca and cb else 0
        piso = max(0, sep - vao)

        item["laco_furos"] = laco
        item["laco_mm"] = round(laco * PITCH_MM, 1)
        item["piso_furos"] = piso
        item["piso_mm"] = round(piso * PITCH_MM, 1)
        item["excesso_furos"] = laco - piso
        item["furos"] = laco
        # `piso` e um limite TEORICO: ignora que o capacitor nao pode ser posto dentro
        # do CI. Num DIP o caminho reto entre os pinos de alimentacao passa por cima do
        # chip, entao o minimo real fica acima do piso. Por isso a folga aceita e de 4
        # furos, e nao zero - cobrar o piso reprovaria posicionamentos que ja sao os
        # melhores possiveis.
        item["piso_teorico"] = True
        item["ok"] = (laco - piso) <= 4
        relatorio_desacopla.append(item)

    layout_json = layout.to_json()
    for pin in layout_json["pins"]:
        pin["net"] = router._net_name(pin["ref"], pin["pin"]) or ""
        pin["label"] = nome_do_furo(pin["col"], pin["row"])

    return {
        "ok": result["stats"]["nets_failed"] == 0 and not result["shorts"] and not layout.problems(),
        "summary": nl.summary(),
        "board": spec.to_json(),
        "layout": layout_json,
        "hole_nets": hole_nets,
        "routes": result["routes"],
        "stats": result["stats"],
        "shorts": result["shorts"],
        "problems": layout.problems(),
        "montagem_problemas": problemas_de_montagem,
        "warnings": fp_warnings,
        "placement": place_report,
        "svg_top": svg_top,
        "svg_bottom": svg_bottom,
        "label_style": style,
        "decoupling": relatorio_desacopla,
        "jumper_limit": relato_jumpers,
        "historico": historico,
        "suggestion": suggestion,
        "build": build_instructions(layout, result, style, plano_bancada, nl),
    }


def build_instructions(layout: Layout, result: dict, style: str = "letra",
                       plano=None, netlist=None) -> dict:
    """Roteiro de montagem: onde por cada peca, que fios, pontes e juntas fazer."""
    nome_do_furo = rotulador(layout.spec, style)
    comps = []
    for ref in sorted(layout.placements):
        fp = layout.footprints.get(ref)
        if not fp or not fp.pins:
            continue
        pl = layout.placements[ref]
        pins = layout.pin_holes(ref)
        comps.append({
            "ref": ref,
            "origin": [pl.col, pl.row],
            "origin_label": nome_do_furo(pl.col, pl.row),
            "rot": pl.rot,
            "pattern": fp.label,
            "pins": {p: list(c) for p, c in sorted(pins.items())},
            "pin_labels": {p: nome_do_furo(c[0], c[1]) for p, c in sorted(pins.items())},
        })

    bridges, top_bridges, jumpers, vias = [], [], [], []
    for r in result["routes"]:
        for seg in r["segments"]:
            item = {
                "net": r["name"],
                "from": seg["from"],
                "to": seg["to"],
                "from_label": nome_do_furo(seg["from"][0], seg["from"][1]),
                "to_label": nome_do_furo(seg["to"][0], seg["to"][1]),
                "length_mm": seg["length_mm"],
                "face": seg.get("face", ""),
                "holes": int(round(max(abs(seg["to"][0] - seg["from"][0]),
                                       abs(seg["to"][1] - seg["from"][1])))) + 1,
            }
            tipo = seg["type"]
            if tipo == "trace":
                bridges.append(item)
            elif tipo == "trace_top":
                top_bridges.append(item)
            elif tipo == "via":
                vias.append(item)
            else:
                item["cut_mm"] = round(seg["length_mm"] + 8, 1)
                jumpers.append(item)

    bridges.sort(key=lambda b: (b["net"], b["from"]))
    top_bridges.sort(key=lambda b: (b["net"], b["from"]))
    vias.sort(key=lambda b: (b["net"], b["from"]))
    jumpers.sort(key=lambda b: (-b["length_mm"], b["net"]))

    # O mesmo roteamento contado como se monta: fio reto, ponte de solda entre
    # vizinhos e junta nas quinas. Quem le com o ferro na mao precisa disto, nao
    # da lista de passos do roteador. Vem pronto de quem desenhou, para desenho e
    # texto contarem a mesma historia.
    if plano is None:
        pinos_da_placa = {}
        for ref in layout.placements:
            for pino, cel in layout.pin_holes(ref).items():
                pinos_da_placa[tuple(cel)] = (ref, pino)
        plano = bancada.plano_de_montagem(result["routes"], nome_do_furo,
                                          pinos_da_placa)

    return {
        "components": comps,
        "bancada": plano,
        # o que fazer, em que ordem: peca baixa primeiro, depois fiacao rede a rede
        "roteiro": manual.monta_roteiro(layout, plano, result["routes"], nome_do_furo,
                                        result.get("stats"), netlist),
        "bridges": bridges,
        "top_bridges": top_bridges,
        "vias": vias,
        "jumpers": jumpers,
        "totals": {
            "bridges": len(bridges),
            "top_bridges": len(top_bridges),
            "vias": len(vias),
            "jumpers": len(jumpers),
            "bridge_mm": round(sum(b["length_mm"] for b in bridges), 1),
            "top_bridge_mm": round(sum(b["length_mm"] for b in top_bridges), 1),
            "jumper_mm": round(sum(j["length_mm"] for j in jumpers), 1),
            "wire_cut_mm": round(sum(j["cut_mm"] for j in jumpers), 1),
        },
    }
