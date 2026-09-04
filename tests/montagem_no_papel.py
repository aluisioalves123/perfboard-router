# -*- coding: utf-8 -*-
"""Monta a placa SEGUINDO O MANUAL e confere se deu o circuito da netlist.

Nao le o roteamento: le o texto do roteiro, o mesmo que a pessoa tem na mao com o
ferro. Cada acao descrita une ilhas de cobre; no fim, cada rede tem de estar num
pedaco so e nenhum pedaco pode conter duas redes.

Existe porque as conferencias anteriores olhavam o PLANO, e o plano estava certo -
o que estava errado era o que o manual mandava fazer com ele. Tres defeitos passaram
assim: 8 pontes que nao apareciam em passo nenhum, 16 vias nunca mencionadas e
pontes sem dizer de que face. Nenhum aparecia olhando o plano; todos aparecem aqui,
porque aqui a placa e montada com o que o texto manda, e so com isso.
"""
import re


class Uniao:
    """Union-find: junta ilhas de cobre conforme o manual manda soldar."""

    def __init__(self):
        self.pai = {}

    def acha(self, x):
        self.pai.setdefault(x, x)
        while self.pai[x] != x:
            self.pai[x] = self.pai[self.pai[x]]
            x = self.pai[x]
        return x

    def une(self, a, b):
        ra, rb = self.acha(a), self.acha(b)
        if ra != rb:
            self.pai[ra] = rb


def _caminho(a, b):
    """Furos que um FIO NU cobre, de ponta a ponta.

    Vale so para fio sem capa: ele deita sobre cada ilha do caminho e faz contato
    com todas. Jumper isolado sobrevoa e nao encosta em nada no meio - quem chamar
    para um jumper tem de usar so as duas pontas.
    """
    (c0, r0), (c1, r1) = a, b
    dc, dr = c1 - c0, r1 - r0
    if dc and dr:
        return [a, b]
    n = max(abs(dc), abs(dr))
    uc, ur = (dc > 0) - (dc < 0), (dr > 0) - (dr < 0)
    return [(c0 + uc * k, r0 + ur * k) for k in range(n + 1)]


# O guia escreve ponte de solda de duas formas. Quando ela nasce da junta que o
# proprio passo esta fazendo, sai como "na MESMA solda de X". Quando varias pontes
# ligadas formam um cordao unico, sai a lista de pares de uma vez - porque na
# bancada aquilo e uma poca de estanho so, nao varias operacoes.
_UMA = re.compile(r"na MESMA solda de ([A-Z]+\d+), puxe a ponte at\S+ "
                  r"([A-Z]+\d+) (por cima|por baixo)")
_CORDAO = re.compile(r"de uma vez só, (por cima|por baixo)[^:]*: (.+) \(rede ")
_PAR = re.compile(r"([A-Z]+\d+)\+([A-Z]+\d+)")


def pontes_do_item(texto):
    """Pares de furos que este item manda ligar, e de que face."""
    m = _UMA.match(texto)
    if m:
        a, b, lado = m.groups()
        return [(a, b, 1 if lado == "por cima" else 0)]
    m = _CORDAO.match(texto)
    if m:
        face = 1 if m.group(1) == "por cima" else 0
        return [(a, b, face) for a, b in _PAR.findall(m.group(2))]
    return []


def replay(roteiro, coord_de, so_por_baixo=(), faces=2):
    """Executa o manual no papel. Devolve (uniao, {(ref,pino): no}).

    Em placa de UMA face o furo tem uma ilha so: cima e baixo sao o mesmo cobre, e
    tratar como dois separava redes que na placa estao juntas.
    """
    u = Uniao()
    pinos = {}
    so_por_baixo = set(so_por_baixo)

    def ilha(furo, face):
        return (furo[0], furo[1], face if faces > 1 else 0)

    for p in roteiro:
        titulo, det = p["titulo"], p["detalhe"]

        if titulo.startswith("Coloque "):
            ref = titulo.split()[1]
            for item in p["itens"]:
                m = re.match(r"pino (\S+) no furo ([A-Z]+\d+)$", item)
                if not m:
                    continue
                pino, rotulo = m.groups()
                furo = coord_de[rotulo]
                no = ("pino", ref, pino)
                pinos[(ref, pino)] = no
                u.une(no, ilha(furo, 0))
                # terminal que aceita solda em cima liga as duas ilhas do furo;
                # sob capacitor ou CI o corpo tampa o furo e so a de baixo vale
                if (ref, pino) not in so_por_baixo:
                    u.une(no, ilha(furo, 1))

        elif titulo.startswith("Via no furo "):
            furo = coord_de[titulo.split()[3]]
            u.une(ilha(furo, 0), ilha(furo, 1))

        elif titulo.startswith("Fio de ") or titulo.startswith("Jumper de "):
            isolado = titulo.startswith("Jumper de ")
            face = 1 if "lado dos componentes" in det else 0
            m = re.search(r"de ([A-Z]+\d+)(.*?) a ([A-Z]+\d+)(.*)$", det)
            assert m, "nao entendi o fio: " + det
            a, resto_a, b, resto_b = m.groups()
            # o jumper tem capa: encosta so onde e soldado, nas duas pontas
            trilho = ([coord_de[a], coord_de[b]] if isolado
                      else _caminho(coord_de[a], coord_de[b]))
            anterior = None
            for furo in trilho:
                if anterior is not None:
                    u.une(ilha(anterior, face), ilha(furo, face))
                anterior = furo
            # ponta marcada como travessia: o proprio fio passa pelo buraco
            for ponta, resto in ((coord_de[a], resto_a), (coord_de[b], resto_b)):
                if "outro lado" in resto:
                    u.une(ilha(ponta, 0), ilha(ponta, 1))

        for item in p["itens"]:
            for a, b, face in pontes_do_item(item):
                u.une(ilha(coord_de[a], face), ilha(coord_de[b], face))

    return u, pinos


def problemas(roteiro, coord_de, netlist, so_por_baixo=(), faces=2):
    """Lista o que sai errado ao montar so com o que o manual manda fazer."""
    u, pinos = replay(roteiro, coord_de, so_por_baixo, faces)
    achados = []

    rede_do_pino = {}
    for net in netlist.nets:
        for ref, pino in net.nodes:
            rede_do_pino[(ref, pino)] = net.name

    for net in netlist.nets:
        nos = [pinos[(r, p)] for r, p in net.nodes if (r, p) in pinos]
        if len(nos) < 2:
            continue
        pedacos = {u.acha(n) for n in nos}
        if len(pedacos) > 1:
            achados.append("a rede %s ficou em %d pedaços separados"
                           % (net.name, len(pedacos)))

    por_pedaco = {}
    for chave, no in pinos.items():
        por_pedaco.setdefault(u.acha(no), set()).add(rede_do_pino.get(chave))
    for redes in por_pedaco.values():
        redes = {r for r in redes if r}
        if len(redes) > 1:
            achados.append("curto: %s no mesmo cobre" % ", ".join(sorted(redes)))

    return achados


def pinos_so_por_baixo(lib):
    """Pinos que so aceitam solda pelo lado de baixo — a mesma regra do roteador."""
    saida = set()
    for ref, fp in lib.items():
        if getattr(fp, "estorva", True):
            for pino in fp.pins:
                saida.add((ref, pino))
    return saida
