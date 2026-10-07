"""Senhas, sessão, proteção CSRF e controle de acesso por papel."""
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from . import config, db

ITERACOES = 240_000


def gerar_hash(senha: str) -> str:
    sal = secrets.token_hex(16)
    calculo = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), ITERACOES).hex()
    return f"pbkdf2_sha256${ITERACOES}${sal}${calculo}"


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        _, iteracoes, sal, calculo = guardado.split("$")
    except ValueError:
        return False
    teste = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), int(iteracoes)).hex()
    return hmac.compare_digest(teste, calculo)


def senha_aceitavel(senha: str) -> str | None:
    """Devolve o motivo da recusa, ou None se a senha serve."""
    if len(senha) < 10:
        return "A senha precisa ter pelo menos 10 caracteres."
    if senha.isdigit() or senha.isalpha():
        return "Use letras e números na senha."
    return None


def ip_do_usuario(request: Request) -> str:
    ip = request.client.host if request.client else ""
    if config.CONFIAR_PROXY and ip in ("127.0.0.1", "::1"):
        ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", ip).split(",")[0].strip()
    return ip


def usuario_atual(request: Request):
    usuario_id = request.session.get("usuario_id")
    if not usuario_id:
        return None
    usuario = db.consultar_um(
        "SELECT u.*, f.codigo AS filial_codigo, f.nome AS filial_nome, f.regiao_id "
        "FROM usuarios u LEFT JOIN filiais f ON f.id = u.filial_id WHERE u.id = ? AND u.ativo = 1",
        (usuario_id,),
    )
    if usuario is None:
        request.session.clear()
    return usuario


class PrecisaLogin(Exception):
    pass


def exigir(request: Request, *papeis: str):
    usuario = usuario_atual(request)
    if usuario is None:
        raise PrecisaLogin()
    if papeis and usuario["papel"] not in papeis and usuario["papel"] != "admin":
        raise HTTPException(status_code=403, detail="Seu usuário não tem acesso a esta tela.")
    return usuario


def token_csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def conferir_csrf(request: Request, enviado: str | None) -> None:
    esperado = request.session.get("csrf")
    if not esperado or not enviado or not hmac.compare_digest(esperado, enviado):
        raise HTTPException(status_code=400, detail="Formulário expirado. Recarregue a página e tente de novo.")


def tentativas_falhas_recentes(ip: str, minutos: int = 15) -> int:
    linha = db.consultar_um(
        "SELECT COUNT(*) AS n FROM eventos WHERE acao = 'login' AND sucesso = 0 AND ip = ? "
        "AND quando >= datetime('now', 'localtime', ?)",
        (ip, f"-{minutos} minutes"),
    )
    return linha["n"] if linha else 0
