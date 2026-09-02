# Publicar num subdomínio

Os exemplos usam `perfboard.SEUDOMINIO.com.br`. Troque pelo seu domínio.

Arquitetura: **nginx** serve os estáticos e faz TLS; **gunicorn** roda a API num socket unix;
o motor de roteamento é Python puro, sem dependência nenhuma.

```
navegador ──HTTPS──> nginx ──┬──> /opt/perfboard/web        (HTML/CSS/JS, direto do disco)
                             └──> unix socket ──> gunicorn ──> deploy/wsgi.py ──> perfboard/
```

Não há banco de dados, sessão, login nem estado no servidor: cada requisição carrega a própria
netlist. Reiniciar não perde nada.

---

## 1. DNS do subdomínio

Na zona DNS do seu domínio (no hPanel da Hostinger, por exemplo), crie:

| Tipo | Nome | Aponta para | TTL |
|---|---|---|---|
| A | `perfboard` | IP da sua VPS | 3600 |

Confira antes de seguir (propaga em minutos, às vezes mais):

```bash
dig +short perfboard.SEUDOMINIO.com.br
```

## 2. Preparar a VPS

```bash
sudo apt update && sudo apt install -y python3-venv nginx
sudo useradd --system --home /opt/perfboard --shell /usr/sbin/nologin perfboard
sudo mkdir -p /opt/perfboard
sudo chown perfboard:www-data /opt/perfboard
```

## 3. Subir o código

Do seu Windows, mande a pasta inteira (menos lixo local):

```bash
rsync -av --delete --exclude '__pycache__' --exclude '.git' ./perfboard/ usuario@SEU_IP:/tmp/perfboard/
```

Na VPS:

```bash
sudo rsync -a --delete /tmp/perfboard/ /opt/perfboard/
sudo chown -R perfboard:www-data /opt/perfboard
sudo -u perfboard python3 -m venv /opt/perfboard/venv
sudo -u perfboard /opt/perfboard/venv/bin/pip install -r /opt/perfboard/requirements.txt
```

Compile o núcleo em C (opcional, mas deixa o roteamento ~14× mais rápido):

```bash
sudo apt install -y gcc
cd /opt/perfboard/native && sudo -u perfboard ./build.sh
```

Note que o `.dll` compilado no Windows não serve no Linux — por isso compila-se lá.
Sem esse passo o serviço funciona igual, só mais devagar.

Confira que os testes passam lá também:

```bash
cd /opt/perfboard && sudo -u perfboard venv/bin/python -m unittest discover -s tests
```

## 4. Serviço

```bash
sudo cp /opt/perfboard/deploy/perfboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now perfboard
systemctl status perfboard --no-pager
```

## 5. nginx + HTTPS

```bash
sudo cp /opt/perfboard/deploy/nginx-perfboard.conf /etc/nginx/sites-available/perfboard
sudo ln -sf /etc/nginx/sites-available/perfboard /etc/nginx/sites-enabled/perfboard
sudo nginx -t && sudo systemctl reload nginx
```

Com a porta 80 no ar e o DNS já resolvendo, peça o certificado:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d perfboard.SEUDOMINIO.com.br
```

O certbot reescreve o arquivo com o bloco TLS e o redirect de 80 → 443, e cuida da renovação.

## 6. Conferir

```bash
curl -s https://perfboard.SEUDOMINIO.com.br/api/health
```

Deve responder `{"ok": true, ...}`.

---

## Atualizar depois

```bash
rsync -av --delete --exclude '__pycache__' ./perfboard/ usuario@SEU_IP:/tmp/perfboard/
ssh usuario@SEU_IP 'sudo rsync -a --delete --exclude venv /tmp/perfboard/ /opt/perfboard/ \
  && sudo chown -R perfboard:www-data /opt/perfboard \
  && sudo systemctl restart perfboard'
```

O `--exclude venv` é importante: o rsync apagaria o ambiente virtual.

---

## Limites de carga (e por que existem)

Roteamento é CPU-bound e o custo cresce com o número de componentes — medido em ~0,16 s por
componente no esforço alto. Sem freio, uma netlist grande com "Tentativas 5" prenderia um worker
por minutos, e meia dúzia de requisições assim derrubariam o serviço. Os freios estão em
`deploy/wsgi.py`:

| Limite | Valor | O que faz |
|---|---|---|
| `MAX_CORPO` | 2 MB | tamanho máximo da netlist |
| `MAX_COMPONENTES` | 400 | acima disso, recusa com mensagem clara |
| `MAX_FUROS` | 8000 | área máxima da placa |
| `ALVO_SEGUNDOS` | 20 s | orçamento para dimensionar esforço |
| `ORCAMENTO_BUSCA` | 90 s | teto de tempo da busca sem limite (`PERFBOARD_ORCAMENTO_S`) |
| `MAX_NUCLEOS` | vCPU ÷ 2, máx. 4 | processos que **uma** busca pode abrir |
| `MAX_SIMULTANEOS` | núcleos − 1 | roteamentos ao mesmo tempo; excedente espera 2 s e leva 503 |
| `TAXA_POR_MINUTO` | 20 (rajada 8) | balde de fichas por IP |

Quando o pedido é pesado demais, o servidor **rebaixa** esforço e tentativas até caber no
orçamento e devolve o que mexeu em `server_notes` — o usuário recebe um resultado um pouco pior
em vez de um erro seco. Só recusa mesmo acima dos tetos duros.

Há uma segunda camada no nginx (`limit_req` 30/min, `limit_conn` 4) e uma terceira no gunicorn
(`timeout 150`, que mata worker travado — tem que ser maior que `ORCAMENTO_BUSCA`, senão ele mata
a busca antes de ela entregar o melhor que achou).

### Duas contas antes de compartilhar o link

**1. Quantas pessoas cabem ao mesmo tempo.** Worker do gunicorn é síncrono: atende um pedido por
vez. E uma busca sem limite segura o worker até o orçamento acabar. Então:

```
buscas simultâneas = PERFBOARD_WORKERS
processos totais   = PERFBOARD_WORKERS × MAX_NUCLEOS   (tem que caber nas vCPUs)
```

Numa VPS de 2 vCPU: 2 workers × 1 núcleo = 2 pessoas buscando ao mesmo tempo. A terceira espera
2 s e leva `503`. Numa de 4 vCPU: 2 × 2. Não adianta subir `PERFBOARD_WORKERS` sem subir vCPU —
só multiplica processo brigando pela mesma CPU.

**2. O resultado é pior no servidor do que local.** Local a busca vai até estacionar sozinha, o que
pode levar minutos e centenas de tentativas. Com orçamento de 90 s ela entrega o melhor que achou
até ali — completo, mas provavelmente não o mais fácil de montar. O servidor avisa isso em
`server_notes`. Para o resultado bom, roda local.

### Se ficar lento

Baixe `ALVO_SEGUNDOS` para 10 e `PERFBOARD_ORCAMENTO_S` para 45 no `perfboard.service`. O
resultado fica um pouco pior nos projetos grandes, mas o site continua responsivo.

---

## Segurança

**Não há autenticação.** Quem tiver o link roda buscas na sua CPU. Os limites acima evitam que uma
pessoa derrube o serviço, mas não impedem que estranhos consumam a máquina. Se o link é para um
grupo conhecido, o mais simples é uma senha no nginx:

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.perfboard seu_usuario
```

```nginx
# dentro do server { } do nginx-perfboard.conf
auth_basic           "Perfboard";
auth_basic_user_file /etc/nginx/.perfboard;
```

- O serviço roda como usuário sem shell, com `ProtectSystem=strict` e sem acesso à rede
  (`RestrictAddressFamilies=AF_UNIX`): ele só fala pelo socket unix.
- Nenhuma entrada do usuário vira caminho de arquivo, comando ou `eval`. O parser de netlist é
  recursivo e puro; `/api/example/` só aceita nome de arquivo simples dentro de `examples/`
  (tem teste para travessia de diretório).
- Erro interno nunca devolve stack trace ao cliente — vai só para o log.
- Não há upload persistido: a netlist vive na memória durante a requisição e some.

---

# Variante: atrás de um proxy que já existe, num subcaminho

O roteiro acima assume uma máquina só para o Perfboard: nginx do lado de fora e
systemd do lado de dentro. Quando o servidor já é compartilhado com outros
projetos, e a porta 443 já tem dono, o caminho é outro:

```
navegador ──HTTPS──> proxy de borda (já existe) ──> web (caddy) ──┬──> /app/web  (estáticos)
                     hardware.EXEMPLO.com.br/perfboard-router     └──> api (gunicorn)
```

Os arquivos desta variante são `Dockerfile`, `docker-compose.yml` e
`Caddyfile.perfboard`, todos aqui em `deploy/`. Sobe com:

```bash
cd deploy && docker compose up -d
```

## Por que um compose próprio, e não um serviço no compose do vizinho

Projeto separado sobe, cai e atualiza sem nunca tocar no que mais roda na
máquina. O único ponto de contato é a rede do proxy de borda, declarada
`external:` — este compose **usa**, nunca cria nem remove. E só o container
`web` entra nela, com apelido explícito: sem isso o nome dele naquela rede
compartilhada seria `web`, genérico demais.

## O subcaminho exige caminho relativo no front

Servir em `/perfboard-router/` em vez da raiz de um subdomínio parece detalhe de
proxy, mas quebra o front se ele referenciar `/style.css`, `/app.js` e
`/api/solve` com a barra inicial: tudo isso resolve na **raiz do domínio** e
volta 404. Por isso `index.html` e `app.js` usam caminho relativo
(`style.css`, `api/solve`), que funciona nos dois lugares — na raiz durante o
desenvolvimento, e sob o prefixo em produção.

⚠️ **Caminho relativo exige a barra final.** Em `/perfboard-router` (sem barra),
`api/solve` resolveria para `/api/solve`, na raiz do domínio. O bloco da borda
redireciona `/perfboard-router` para `/perfboard-router/` justamente por isso.

⚠️ **`handle` e não matcher solto, no Caddy.** A ordem padrão das diretivas
executa `try_files` **antes** de `reverse_proxy`, então `/api/health` era
reescrito para `/index.html` e o cliente recebia a página HTML no lugar do JSON.
Blocos `handle` são mutuamente exclusivos e avaliados na ordem em que aparecem.

⚠️ **`flush_interval -1` nos dois saltos.** A busca transmite progresso enquanto
roda; com o buffer do proxy, a tela do usuário só se mexeria no fim, e uma busca
difícil leva dezenas de segundos.

## O código não entra na imagem

A imagem tem só o interpretador e o gunicorn; o código chega por bind mount
somente-leitura. Atualizar é `git pull` e reiniciar, sem rebuild:

```bash
cd /opt/perfboard && git pull
cd deploy && docker compose restart api
```

O `gcc` fica na imagem de propósito: o núcleo em C é compilado **lá dentro**,
contra a mesma libc que vai executá-lo. Binário compilado no host pode exigir uma
glibc mais nova que a da imagem e simplesmente não carregar — e a falha é
silenciosa, porque o núcleo em C é opcional e o programa cai no Python puro sem
avisar. Depois de mexer em `native/`:

```bash
docker run --rm -v /opt/perfboard:/app -w /app/native perfboard:latest sh ./build.sh
cd deploy && docker compose restart api
```

## Teto de CPU não é opcional em máquina compartilhada

`os.cpu_count()` **não enxerga** limite de container: numa máquina de 8 vCPU o
app continua achando que tem as 8, e `MAX_SIMULTANEOS` nasce 7 mesmo com o
container preso em 2. Quem segura de verdade é o `cpus:` do compose, que é teto
de kernel. Sem ele, uma busca grande num endpoint público abre processo até
encher a máquina e os projetos vizinhos ficam lentos sem motivo aparente.
