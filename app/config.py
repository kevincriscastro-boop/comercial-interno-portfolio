"""Configuração lida de variáveis de ambiente (arquivo .env opcional na raiz do projeto)."""
import os
import secrets
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _carregar_env() -> None:
    arquivo = RAIZ / ".env"
    if not arquivo.exists():
        return
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip())


_carregar_env()

PASTA_DADOS = Path(os.environ.get("APP_DADOS", RAIZ / "dados")).resolve()
PASTA_ARQUIVOS = PASTA_DADOS / "arquivos"
PASTA_BACKUP = PASTA_DADOS / "backup"
BANCO = PASTA_DADOS / "precos.db"

# Chave das sessões. Em produção, defina APP_SEGREDO no .env (senão os logins caem a cada reinício).
SEGREDO = os.environ.get("APP_SEGREDO") or secrets.token_hex(32)
SEGREDO_FIXO = "APP_SEGREDO" in os.environ

# Atrás do nginx, o IP real do usuário vem no cabeçalho X-Real-IP.
CONFIAR_PROXY = os.environ.get("APP_CONFIAR_PROXY", "1") == "1"

# Cookie só por HTTPS (ligar na VPS; desligado para testar em http://localhost).
COOKIE_SEGURO = os.environ.get("APP_COOKIE_SEGURO", "0") == "1"

TAMANHO_MAX_ARQUIVO = 20 * 1024 * 1024  # 20 MB

# Nome da empresa no cabeçalho e no título das páginas (opcional).
EMPRESA = os.environ.get("APP_EMPRESA", "").strip()

for pasta in (PASTA_DADOS, PASTA_ARQUIVOS, PASTA_BACKUP):
    pasta.mkdir(parents=True, exist_ok=True)
