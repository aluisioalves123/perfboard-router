# Perfboard Router

Pega a **netlist do KiCad** (`.net`) e as **dimensões da sua perfboard**, e gera o posicionamento
das peças, as **trilhas do lado da solda** e os **jumpers do lado dos componentes** — respeitando
as restrições reais de uma placa perfurada de 0,1" (2,54 mm).

Roda 100% local, sem instalar nada além do Python 3.9+.

---

## Por que isso é difícil

Roteador de PCB comum tem o plano contínuo: a trilha vai a qualquer lugar, com qualquer largura,
e vias custam centavos. Perfboard não tem nada disso.

- A trilha só anda de **furo em furo ortogonalmente vizinho**. Não existe diagonal, curva, nem
  meio-passo.
- Cada furo é **exclusivo de uma rede**. Duas trilhas da mesma face só poderiam se cruzar dentro
  de um furo — e não podem.
- O terminal de um componente **atravessa** a placa e ocupa as duas faces daquele furo.
- Quem monta é uma pessoa com ferro de solda. Então o que custa caro não é comprimento de fio:
  é **cada dobra, cada via e cada jumper**.

Esse último ponto muda o objetivo inteiro. Um roteador de PCB minimiza área e comprimento; aqui a
função de custo é o **trabalho de bancada**.

---

## Como rodar

```bash
python server.py
```

O navegador abre em `http://127.0.0.1:8765`. No Windows dá para usar o atalho `iniciar.bat`.

> Esse servidor embutido é o `http.server` da stdlib, **só para uso local**. Para publicar,
> veja [deploy/README.md](deploy/README.md): nginx + gunicorn, com limites de carga e taxa.

### Núcleo em C, opcional

O A\* do roteador e o recozimento do posicionador têm versão em C em [native/](native/), ligada
por `ctypes`. Compilar é opcional — sem ela tudo funciona igual, só mais devagar:

```bash
cd native && ./build.sh          # ou build.bat no Windows
```

Precisa só de `gcc`, não dos cabeçalhos do Python. Se a biblioteca não existir, os dois caem
sozinhos na implementação em Python, que continua sendo a referência de corretude — é contra ela
que o C é medido nos testes.

| | Python | C |
|---|---|---|
| Arrastar uma peça (rotear de novo) | 0,676 s | **0,048 s** (14×) |
| Posicionar (26 peças, esforço alto) | 4,19 s | **0,06 s** (70×) |

### Nada de IA em tempo de execução

O motor é 100% algorítmico. Não há chamada de API, chave, nem acesso à rede — os imports do
projeto são todos da biblioteca padrão (`heapq`, `math`, `random`, `re`, `json`, `ctypes`,
`multiprocessing`, `http.server`). Hospedar não gera custo por uso.

Isso **não** quer dizer que ele seja reprodutível: a busca é estocástica, e a ordem de iteração de
conjuntos de strings no Python muda a cada processo. Rodar duas vezes com os mesmos parâmetros dá
resultados diferentes — geralmente equivalentes, às vezes um melhor que o outro. É por isso que a
busca insiste em vez de tentar uma vez só.

---

## Como usar

1. No KiCad: **Esquemático → Arquivo → Exportar → Netlist → KiCad** (gera o `.net`).
2. Arraste o `.net` na caixa da esquerda.
3. Informe **colunas × linhas** da sua placa. Uma perfboard marcada de `A` a `R` e de `1` a `24`
   é **24 colunas × 18 linhas**.
4. Clique em **Posicionar + rotear**.

Depois disso:

- **arraste** qualquer peça no desenho — ele reroteia sozinho;
- **`R`** gira 90°, **`L`** trava a peça no lugar (peça travada o posicionador não move);
- o botão **⤢** na lista de componentes ajusta o tamanho do corpo da peça real;
- exporte **SVG dos dois lados**, **JSON do projeto** e o **guia de montagem em texto**.

O trabalho fica guardado no navegador: recarregar a página não perde nada.

---

## A busca

### Primeiro fechar, depois facilitar

Um layout com pino solto não serve para nada — não dá para montar meio circuito. Então a busca tem
duas etapas, com objetivos diferentes:

**1. Achar uma solução completa.** Cada tentativa é um sorteio independente: posiciona com uma
semente nova e roteia. Não há número máximo de tentativas; ele insiste até fechar 100% ou até você
apertar **Parar**, que devolve o melhor encontrado até ali.

**2. Achar uma solução mais fácil de montar.** Depois da primeira que fecha, ele continua — agora
comparando soluções completas entre si pelo **trabalho de bancada**:

```
esforço = turn_cost × quinas + via_cost × vias + jumper_base × jumpers + trace_cost × furos
```

Os pesos são os mesmos que guiaram cada trilha (o perfil escolhido em *Onde economizar*). Otimizar
com um critério e ranquear com outro não faria sentido.

### Quando parar de insistir

A melhora não é regular. No circuito de teste ela veio nas tentativas 1, 13, 105, 161 e 698 —
quem desistisse no primeiro silêncio longo perderia as duas últimas. Por isso o critério de parada
tem duas partes:

- conta **soluções completas** sem ganho, não tentativas: uma tentativa que nem fecha não diz nada
  sobre a qualidade ter estacionado (num caso real só ~5% das tentativas fecham);
- o limite acompanha o **maior silêncio já vencido**: se ele já aguentou 20 soluções sem ganho e a
  21ª melhorou, aguenta de novo.

Durante a busca a tela mostra a curva subindo e quanto já foi economizado, para você decidir se
vale continuar. Depois de pronto o gráfico some — o número só serve enquanto a decisão é sua.

### Uma tentativa por núcleo

As tentativas são independentes, então elas rodam em processos paralelos. Numa máquina de 12
núcleos, a vazão vai de **1,15 para 6,3 tentativas por segundo**.

O paralelismo é por **processo**, não thread: o trabalho é CPU pura em Python, e o GIL faria as
threads se revezarem no mesmo núcleo. Uma tentativa isolada (arrastar uma peça e reroteiar) roda no
próprio processo — abrir um pool custaria mais que o trabalho.

---

## O modelo físico

Cada furo tem **duas ilhas**: a de baixo (lado da solda) e a de cima (lado dos componentes). O que
dá para fazer com elas depende do **tipo de placa**.

### Perfboard de 1 face

As duas ilhas do furo são, na prática, o mesmo ponto: solda feita por cima escorre e encosta na
ilha de baixo. O furo inteiro pertence a uma rede só, e o lado dos componentes só aceita **jumper**
— fio isolado que pousa apenas nas pontas.

| Recurso | Onde fica | Regra |
|---|---|---|
| **Furo** | atravessa a placa | de uma rede só; o terminal do componente já reserva o furo |
| **Trilha** | lado da solda | ponte de solda / fio nu entre furos **ortogonalmente vizinhos** |
| **Jumper** | por cima | fio isolado em reta entre dois furos; sobrevoa tudo, só consome as pontas |

Um furo comporta o terminal **ou** a ponta do jumper, nunca os dois: na bancada não dá para enfiar
o fio no mesmo furo que já tem a perna do componente soldada.

### Perfboard de 2 faces

Sem furo metalizado ligando os lados, as ilhas de cima e de baixo são eletricamente
**independentes**. Aí existe uma segunda camada de verdade:

| Recurso a mais | O que é |
|---|---|
| **Trilha por cima** | fio nu no lado dos componentes, de furo em furo, igual à de baixo |
| **Via** | pedaço de fio atravessando um furo livre, soldado **nas duas faces** |

É assim que duas redes se cruzam sem encostar: uma passa por baixo, a outra por cima. Na prática
**dispensa jumper**.

Duas ressalvas físicas. O terminal de um componente ocupa as **duas** faces do furo — e já serve de
via para a rede dele, de graça. E trilha por cima **não passa por baixo do corpo das peças**: a peça
está fisicamente no caminho.

### Nem todo pino aceita solda por cima

A distinção que mais importa na bancada, e a que mais custou para o modelo acertar: depende do
**tipo da peça**, não do espaço em volta.

| | O corpo fica | Solda por cima do furo |
|---|---|---|
| resistor, diodo axial, indutor | **entre** os terminais | sim — o furo continua livre |
| transistor TO-92 | acima, mas pequeno e com pernas abertas | sim |
| capacitor | **sobre** os terminais | **não** — a cerâmica cobre o furo |
| CI | em socket, cobrindo os pinos | **não** |
| borne, trimpot | corpo alto sobre os furos | **não** |

Para os que não aceitam, não é questão de apertado: **nenhuma placa maior resolve**. Esses ligam-se
só pelo lado da solda, e o roteador trata isso como restrição dura.

Antes de chegar nisso, o modelo tentou três versões geométricas — proibir a face de cima, exigir um
anel de folga em volta das peças, contar furos livres em volta de cada solda. Todas erravam do mesmo
jeito: penalizavam o resistor, que é a maioria da placa e justamente a peça que **não** atrapalha. A
pergunta certa nunca foi "cabe o ferro aqui?", e sim "o corpo desta peça está em cima do furo dela?".

### Fio reto, ponte de solda e junta

Uma quina **não** é um fio dobrado. Dobrar fio fino no ângulo certo, no lugar certo, é briga perdida
— o que se faz são dois fios retos e estanho no furo entre eles. Por isso o desenho e o guia falam
em três coisas, e só três:

| | O que é | Como aparece no desenho |
|---|---|---|
| **Ponte de solda** | dois furos vizinhos, **sem fio nenhum** | traço curto e grosso |
| **Fio reto** | pedaço cortado no tamanho, soldado nas duas pontas | linha fina com bolinha em cada ponta |
| **Junta** | onde dois trechos se encontram | anel em volta do furo |

O estanho alcança o furo do meio e os vizinhos ortogonais — no máximo uma cruz, na prática quase
sempre um L. O sistema avisa se alguma junta precisasse de mais que isso.

Desenho e guia leem a **mesma** lista (`perfboard/bancada.py`): não existe a possibilidade de um
dizer 13 juntas e o outro 14.

### Quando alguma rede não fecha

O resultado nunca é "quase certo em silêncio". Depois de rotear, o sistema **reconstrói as ligações
a partir dos segmentos que você vai realmente soldar** e confere se todos os pinos de cada rede caem
no mesmo grafo. Pino que sobrar:

- ganha **anel e X vermelhos** no desenho dos dois lados;
- aparece nomeado na Verificação (`R6.1 no furo Q12 — rede Net-(J2-Pin_1)`);
- entra no guia de montagem numa seção `!! ATENCAO`;
- deixa os trechos da rede incompleta com **casca vermelha tracejada**.

### Sobre desligar os jumpers

Numa perfboard de 1 face sem jumper, toda a fiação tem que caber numa face só — ou seja, o circuito
precisa ser **planar** naquela posição de peças. Circuitos com CI quase nunca são: um LM324 ou um
74HC595 no meio da placa praticamente garante cruzamento.

A saída boa é trocar para **2 faces**. Se ainda assim você quiser 1 face sem jumper, o sistema
roteia de novo com jumper caríssimo e diz o **mínimo de jumpers** que resolveria naquela posição —
*"17 pinos ficam soltos; com 9 jumpers fecha 100%"*. Em vez de entregar um layout furado, ele te dá
o número para decidir.

### Dobra e jumper são substitutos

Fugir de uma joga trabalho na outra; não dá para minimizar as duas. O perfil **Onde economizar**
escolhe de que lado ficar. Medido num circuito de 26 peças em 24×18:

| Perfil | dobras | jumpers |
|---|---|---|
| menos jumpers | 38 | **10** |
| equilibrado | 28 | 13 |
| menos dobras | **18** | 19 |

---

## Algoritmos

**Posicionamento** — empacotamento guloso + *simulated annealing*, minimizando comprimento estimado
das ligações (HPWL), sobreposição de corpos, furos fora da placa e **pinos sufocados** (furo cercado
por pinos de outras redes, do qual nenhuma trilha consegue sair, em face nenhuma). Peças travadas
ficam fixas; conectores (`J*`, `P*`) são puxados para a borda.

Sobreposição de corpos é **proibida**, não cara: na prática é impossível montar. A penalidade
começa frouxa e endurece ao longo do recozimento — proibir desde o primeiro passo transforma a
restrição num muro que impede a busca de atravessar o terreno ruim até o terreno bom.

**Espalhar também é objetivo.** Minimizar comprimento de fio, sozinho, junta tudo num canto — e
numa perfboard esse é o pior resultado possível: a placa já foi cortada nesse tamanho, então sobra
de área não economiza nada e só tira espaço do ferro, da trilha de cima e da via. A placa é dividida
em regiões 3×3 e o custo cresce com o **quadrado** do desequilíbrio de ocupação entre elas, o que
faz uma região lotada ao lado de uma vazia pesar muito mais que várias um pouco acima da média.

**Capacitor de desacoplamento** pode ser vinculado ao CI que ele protege. O posicionador minimiza a
área do laço entre o pino de alimentação do CI e o capacitor, sem deixar um por cima do outro.

**Roteamento** — cada rede vira uma árvore construída estilo Prim, com **A\*** multi-origem /
multi-destino sobre o grafo `(coluna, linha, face, direção de chegada)`. A direção entra no estado
porque dobrar o fio custa: sem ela o algoritmo não saberia que acabou de virar. Duas fases:

1. **Negociação de congestionamento (PathFinder).** Todas as redes pegam seu melhor caminho, mesmo
   pisando no do vizinho. A cada rodada o recurso disputado fica mais caro (fator de presença) e
   guarda rancor permanente (custo de histórico), até sobrar no máximo um dono por recurso. É bem
   melhor que rip-up com regra dura, onde uma rede azarada simplesmente trava.
2. **Legalização estrita.** Se a negociação não converge, cai no modo com restrição dura já
   carregando o histórico aprendido, e devolve a melhor solução **legal** encontrada.

A saída é auditada de forma independente antes de sair, então nunca sai layout ilegal nem pino
solto em silêncio.

---

## Medido num circuito real

LM324 + 2 transistores + 15 resistores + 3 capacitores + 4 bornes + trimpot (26 peças / 17 redes
a rotear), **2 faces, sem jumper nenhum**, com as regras físicas todas ativas — inclusive os 31
pinos de capacitor, CI, borne e trimpot que só aceitam ligação pelo lado da solda.

| placa | fecha 100% | fios retos | vias | juntas | achou na tentativa |
|---|---|---|---|---|---|
| 24×18 | não (2 soltos) | — | — | — | — |
| **28×22** | **sim** | **53** | **4** | **38** | **5** |
| 32×26 | sim | 91 | 31 | 62 | 18 |

Repare que a placa maior saiu **pior**: mais espaço deu mais volta, não menos. E que a 24×18 não
fecha — 26 peças com 15 resistores de 4 furos e um DIP-14 não cabem em 432 furos com folga para
rotear.

Depois de fechar, continuar procurando vale a pena: numa medição anterior a busca saiu de 47 quinas
e 15 vias para 37 quinas e 6 vias, **28% menos trabalho de montagem**, e parou sozinha ao estacionar.
Em um processo só isso levava 309 s; com os núcleos em paralelo, 54 s.

### Tamanho de placa não resolve falta de camada

Face única e sem jumper, 8 sementes cada:

| Placa | Melhor resultado |
|---|---|
| 20×15 | 13 pinos soltos |
| 24×18 | 8 pinos soltos |
| 28×20 | 10 pinos soltos |
| 32×24 | 9 pinos soltos |

Aumentar a placa **não ajuda**: o obstáculo é topológico, não de espaço. As mesmas 24×18 em 2 faces
fecham 100%. Camada resolve; área não.

---

## Footprints

O sistema traduz a string de footprint do KiCad para um padrão de furos:

| Footprint | Vira |
|---|---|
| `DIP-8_W7.62mm`, `DIP-14`, `DIP-16`… | duas fileiras, largura tirada do `_W…mm` |
| `PinHeader_1xNN` / `2xNN_P2.54mm` | 1 ou 2 colunas de pinos |
| `R_Axial…_P7.62mm`, `C_Disc…_P5.00mm`, `LED_D…_P2.54mm` | 2 terminais, passo = `pitch / 2,54` arredondado |
| `TO-92`, `TO-220`, `SOT-223` | 3 pernas em linha |
| `TerminalBlock…` | passo 2 furos (5,08 mm) |
| qualquer outro com `_P<x>mm` | pinos em linha com esse passo |

### Tamanho do corpo, separado dos pinos

O footprint do KiCad acerta onde ficam os terminais, mas isso não é o tamanho da peça. Um borne
Phoenix tem os parafusos na frente e o plástico avançando para trás; um eletrolítico D10 ocupa muito
mais que o vão de 5 mm entre as pernas. Por isso o **corpo** é um retângulo próprio, independente do
retângulo dos pinos, e é ele que:

- impede outra peça de ocupar aquele espaço (colisão e posicionamento);
- bloqueia trilha do lado dos componentes em placa de 2 faces;
- entra na conta do tamanho de placa sugerido;
- é o que você vê desenhado.

Corpo passando da **borda** da placa não é defeito: borne e conector de borda são montados
exatamente assim, e do lado de fora não há nada com que colidir.

O tamanho vem das medidas no próprio nome do footprint quando existem (`_D5.0mm`, `_L6.3mm`,
`_W2.5mm`), e de valores típicos de catálogo para borne, TO-220 e trimpot. O botão **⤢** ajusta à
peça real, e o ajuste fica salvo no **Projeto (.json)**.

Pitch que não cai na grade (ex.: 2,50 mm) é arredondado e aparece um aviso. Footprints **SMD** são
sinalizados em vermelho: não cabem em perfboard.

---

## Estrutura

```
perfboard/
  server.py              servidor HTTP local (só stdlib)
  perfboard/
    sexp.py              parser de S-expressions do KiCad
    netlist.py           leitura do .net -> componentes, pinos, redes
    footprints.py        footprint do KiCad -> padrão de furos
    board.py             grade, posicionamento, colisões, nomes de furo (A1…R24)
    placer.py            simulated annealing
    router.py            A* + negociação de congestionamento
    paralelo.py          tentativas em processos paralelos
    nativo.py            ponte ctypes para o núcleo em C
    bancada.py           traduz o roteamento para fio, ponte de solda e junta
    render.py            desenho SVG dos dois lados
    project.py           orquestração, busca e guia de montagem
  web/                   interface (HTML/CSS/JS puro, sem framework)
  examples/              netlists de exemplo
  tests/                 verificação de integridade física do resultado
  deploy/                publicação: WSGI, gunicorn, systemd, nginx
  native/                núcleo em C do A* e do posicionador (opcional)
```

## Testes

```bash
python -m unittest discover -s tests -v
```

67 testes. Eles não checam formato de saída: checam que o resultado é **fisicamente construível** —
nenhum furo ou aresta disputado por duas redes, jumper só entre furos da mesma rede e nunca num furo
que já tem pino, corpos sem sobreposição, e cada rede formando um grafo conexo que contém todos os
seus pinos.

O núcleo em C é comparado contra o Python nos mesmos casos: o roteador tem que dar exatamente o
mesmo resultado, e o posicionador — que é heurístico — tem que respeitar as mesmas invariantes e
ficar dentro de uma tolerância agregada em 20 sementes.

`PERFBOARD_SEM_C=1` desliga o C e roda tudo em Python.

## API

- `POST /api/analyze` → `{netlist}` devolve componentes, footprints deduzidos, avisos e tamanho
  sugerido de placa.
- `POST /api/solve` → `{netlist, board, placements, auto_place, placer, router, label_style}`
  devolve layout, rotas, estatísticas, os dois SVGs e o guia de montagem. Com `stream: true`,
  responde em NDJSON com o progresso da busca.

Ou direto do Python:

```python
from perfboard.project import solve

r = solve({
    "netlist": open("meu.net", encoding="utf-8").read(),
    "board": {"cols": 24, "rows": 18},
    "placer": {"effort": "alto", "modo": "otimizar", "paciencia": 60},
    "router": {"faces": 2, "allow_jumpers": False},
})
print(r["stats"])
open("solda.svg", "w", encoding="utf-8").write(r["svg_bottom"])
```

---

## Limitações conhecidas

- O corpo do componente é um retângulo. Peças com formato irregular merecem conferida visual.
- Não há modelo de corrente nem largura de trilha: para potência, trave as peças e engrosse à mão.
- Não lê `.kicad_sch` nem `.kicad_pcb` — só a netlist exportada.
- O posicionador otimiza comprimento de fiação e evita sufocar pinos, mas **não modela
  congestionamento de canais**. É a maior lacuna do sistema: ele minimiza uma coisa (fio) esperando
  melhorar outra (o roteador fechar). Em placa apertada sem jumper isso pesa.
- Toda tentativa começa do zero. Partir do melhor layout já encontrado e perturbá-lo — em vez de
  sortear tudo de novo — tende a achar vizinhos melhores mais rápido que um sorteio limpo.
