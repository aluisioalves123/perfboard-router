"""Configuracao do gunicorn para o Perfboard Router."""
import multiprocessing
import os

# Escuta num socket unix; quem fala com a internet e o nginx.
bind = os.environ.get("PERFBOARD_BIND", "unix:/run/perfboard/perfboard.sock")

# Roteamento e CPU-bound: workers sincronos. Threads nao ajudariam (o GIL segura).
#
# POUCOS workers de proposito. Cada busca abre ate MAX_NUCLEOS processos proprios
# (veja wsgi.py), entao a conta e `workers x MAX_NUCLEOS <= vCPUs`. Worker demais
# aqui nao aumenta a capacidade: multiplica processos brigando pela mesma CPU.
#
# Worker sincrono atende um pedido por vez, e uma busca sem limite segura o worker
# ate o orcamento acabar. Ou seja: o numero de buscas simultaneas E o numero de
# workers. Numa VPS de 2 vCPU, duas pessoas buscando ao mesmo tempo ja e o teto -
# a terceira espera. Para mais gente, mais vCPU.
workers = int(os.environ.get("PERFBOARD_WORKERS", max(2, multiprocessing.cpu_count() // 2)))
worker_class = "sync"

# Rede de seguranca: se um pedido travar, o worker e morto e reciclado.
# O wsgi.py ja limita a carga para caber bem abaixo disso.
# Tem que ser MAIOR que o PERFBOARD_ORCAMENTO_S do wsgi.py (90s por padrao),
# senao o gunicorn mata a busca antes de ela entregar o melhor que achou.
timeout = int(os.environ.get("PERFBOARD_TIMEOUT", 150))
graceful_timeout = 20
keepalive = 5

# Recicla workers de vez em quando para nao acumular fragmentacao de memoria.
max_requests = 400
max_requests_jitter = 50

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("PERFBOARD_LOGLEVEL", "info")
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(L)ss'

proc_name = "perfboard"
