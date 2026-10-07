"""Registro de eventos (log de auditoria) e guarda de arquivos com hash."""
import hashlib
import uuid
from pathlib import Path

from fastapi import Request

from . import config, db
from .seguranca import ip_do_usuario


def registrar(
    acao: str,
    tipo: str = "acao",
    request: Request | None = None,
    usuario=None,
    sucesso: bool = True,
    detalhes=None,
    filial_id: int | None = None,
    lote_id: int | None = None,
    status: int | None = None,
    duracao_ms: int | None = None,
    login: str | None = None,
) -> None:
    ip = navegador = metodo = rota = None
    if request is not None:
        ip = ip_do_usuario(request)
        navegador = request.headers.get("user-agent", "")[:400]
        metodo = request.method
        rota = str(request.url.path)
        if request.url.query:
            rota += "?" + request.url.query
    db.executar(
        "INSERT INTO eventos (quando, usuario_id, login, ip, navegador, tipo, acao, metodo, rota, status, "
        "duracao_ms, sucesso, filial_id, lote_id, detalhes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            db.agora(),
            usuario["id"] if usuario else None,
            usuario["login"] if usuario else login,
            ip,
            navegador,
            tipo,
            acao,
            metodo,
            rota,
            status,
            duracao_ms,
            1 if sucesso else 0,
            filial_id,
            lote_id,
            db.para_json(detalhes) if detalhes is not None else None,
        ),
    )


def guardar_arquivo(dados: bytes, nome_original: str, tipo: str, usuario_id: int | None, analise=None) -> int:
    """Salva o arquivo em disco com nome único e registra tamanho e SHA-256 no banco."""
    hoje = db.agora()[:7]  # AAAA-MM
    pasta = config.PASTA_ARQUIVOS / hoje
    pasta.mkdir(parents=True, exist_ok=True)
    extensao = Path(nome_original).suffix.lower() or ".xlsx"
    destino = pasta / f"{tipo}_{uuid.uuid4().hex}{extensao}"
    destino.write_bytes(dados)
    return db.executar(
        "INSERT INTO arquivos (tipo, nome_original, caminho, tamanho, sha256, usuario_id, criado_em, analise_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            tipo,
            nome_original,
            str(destino.relative_to(config.PASTA_DADOS)),
            len(dados),
            hashlib.sha256(dados).hexdigest(),
            usuario_id,
            db.agora(),
            db.para_json(analise) if analise is not None else None,
        ),
    )


def caminho_arquivo(arquivo) -> Path:
    return config.PASTA_DADOS / arquivo["caminho"]
