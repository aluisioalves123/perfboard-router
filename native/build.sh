#!/bin/sh
# Compila o nucleo em C. Opcional: sem ele o programa roda igual, so mais devagar.
set -e
cd "$(dirname "$0")"
case "$(uname -s 2>/dev/null || echo Windows)" in
  MINGW*|MSYS*|CYGWIN*|Windows) alvo=perfboard.dll; extra="" ;;
  *)                            alvo=perfboard.so;  extra="-fPIC" ;;
esac
gcc -O2 -Wall -Wextra -shared $extra -o "$alvo" perfboard.c place.c
echo "compilado: $alvo"
