# Núcleo em C (opcional)

Contém só o A\* do roteador — o laço mais quente e o que define a latência quando
você arrasta uma peça na tela. Todo o resto continua em Python.

**Nada aqui é obrigatório.** Se a biblioteca não estiver compilada, o roteador usa a
implementação em Python e o resultado é equivalente; só fica mais lento.

## Compilar

```bash
./build.sh          # Linux, macOS, Git Bash
build.bat           # Windows (cmd)
```

Precisa apenas de `gcc`. **Não** precisa dos cabeçalhos do Python: a ponte é feita
com `ctypes`, então o mesmo `.c` serve para qualquer versão de Python e não quebra
quando você atualiza o interpretador.

Na VPS:

```bash
sudo apt install -y gcc
cd /opt/perfboard/native && ./build.sh
sudo systemctl restart perfboard
```

## Ganho medido

Circuito de 26 peças, placa 24×18:

| Operação | Python | Com C |
|---|---|---|
| Arrastar peça (só roteamento) | 0,676 s | **0,048 s** |
| Importar (posicionar + rotear) | 2,05 s | 1,42 s |

O ganho na importação é menor porque o posicionador (recozimento simulado) continua
em Python e responde por ~65% do tempo. Pela lei de Amdahl, com o roteamento
praticamente zerado o teto de ganho total é ~1,5× — que é o que se observa.

## Conferindo se está ativo

```python
from perfboard import nativo
print(nativo.descricao())
```

Para forçar o caminho em Python (usado nos testes): `PERFBOARD_SEM_C=1`.

## Correção

`tests/test_nativo.py` roda os mesmos casos nas duas implementações e compara. Não
exige caminho idêntico — quando dois caminhos empatam em custo, cada uma pode
escolher o seu — mas exige as mesmas redes fechadas, nenhum pino solto a mais,
nenhum layout ilegal e qualidade dentro de 4% na média.
