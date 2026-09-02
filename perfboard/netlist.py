"""Leitura de netlists exportadas do KiCad (.net, formato S-expression)."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import sexp


@dataclass
class Component:
    ref: str
    value: str = ""
    footprint: str = ""
    lib: str = ""
    part: str = ""
    pins: list = field(default_factory=list)      # numeros de pino, na ordem do libpart
    pin_names: dict = field(default_factory=dict)  # numero -> nome funcional

    @property
    def pin_count(self) -> int:
        return len(self.pins)


@dataclass
class Net:
    code: str
    name: str
    nodes: list = field(default_factory=list)  # lista de (ref, pin)

    @property
    def is_trivial(self) -> bool:
        """Rede com menos de 2 pinos nao precisa de roteamento."""
        return len(self.nodes) < 2


@dataclass
class Netlist:
    components: dict = field(default_factory=dict)  # ref -> Component
    nets: list = field(default_factory=list)
    source: str = ""

    def routable_nets(self):
        return [n for n in self.nets if not n.is_trivial]

    def net_of(self, ref: str, pin: str):
        for n in self.nets:
            if (ref, pin) in n.nodes:
                return n
        return None

    def summary(self) -> dict:
        return {
            "source": self.source,
            "components": len(self.components),
            "pins": sum(c.pin_count for c in self.components.values()),
            "nets": len(self.nets),
            "routable_nets": len(self.routable_nets()),
        }


def parse_netlist(text: str) -> Netlist:
    root = sexp.parse(text)
    if sexp.head(root) != "export":
        raise ValueError("nao parece uma netlist do KiCad (no raiz '%s')" % sexp.head(root))

    nl = Netlist()
    design = sexp.child(root, "design")
    if design is not None:
        nl.source = sexp.value(design, "source", "") or ""

    # --- libparts: de onde vem a lista canonica de pinos de cada simbolo ---
    libpins = {}   # (lib, part) -> [(num, name)]
    libparts = sexp.child(root, "libparts")
    if libparts is not None:
        for lp in sexp.children(libparts, "libpart"):
            key = (sexp.value(lp, "lib", ""), sexp.value(lp, "part", ""))
            pins_node = sexp.child(lp, "pins")
            entries = []
            if pins_node is not None:
                for p in sexp.children(pins_node, "pin"):
                    num = sexp.value(p, "num", "")
                    nam = sexp.value(p, "name", "") or ""
                    if num:
                        entries.append((num, nam))
            libpins[key] = entries

    # --- components ---
    comps_node = sexp.child(root, "components")
    if comps_node is not None:
        for c in sexp.children(comps_node, "comp"):
            ref = sexp.value(c, "ref", "")
            if not ref:
                continue
            src = sexp.child(c, "libsource")
            lib = sexp.value(src, "lib", "") if src is not None else ""
            part = sexp.value(src, "part", "") if src is not None else ""
            comp = Component(
                ref=ref,
                value=sexp.value(c, "value", "") or "",
                footprint=sexp.value(c, "footprint", "") or "",
                lib=lib or "",
                part=part or "",
            )
            for num, nam in libpins.get((comp.lib, comp.part), []):
                comp.pins.append(num)
                comp.pin_names[num] = nam
            nl.components[ref] = comp

    # --- nets ---
    nets_node = sexp.child(root, "nets")
    if nets_node is not None:
        for n in sexp.children(nets_node, "net"):
            net = Net(code=sexp.value(n, "code", "") or "", name=sexp.value(n, "name", "") or "")
            for nd in sexp.children(n, "node"):
                ref = sexp.value(nd, "ref", "")
                pin = sexp.value(nd, "pin", "")
                if ref and pin:
                    net.nodes.append((ref, pin))
                    # componente sem libpart: reconstroi a lista de pinos pelas redes
                    comp = nl.components.get(ref)
                    if comp is not None and pin not in comp.pins:
                        comp.pins.append(pin)
            nl.nets.append(net)

    # ordena pinos numericamente quando possivel (DIP-8: 1..8)
    for comp in nl.components.values():
        comp.pins.sort(key=_pin_sort_key)

    return nl


def _pin_sort_key(pin: str):
    try:
        return (0, int(pin), "")
    except ValueError:
        return (1, 0, pin)
