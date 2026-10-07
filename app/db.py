"""Banco SQLite: esquema, dados iniciais e funções de acesso."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

ESQUEMA = """
-- Região = grupo de filiais que usa a mesma tabela de preço do gerente (ex.: SP, PR).
CREATE TABLE IF NOT EXISTS regioes (
    id      INTEGER PRIMARY KEY,
    codigo  TEXT NOT NULL UNIQUE,
    nome    TEXT NOT NULL
);

-- Filial = uma conta do Mercos (cada uma exporta e importa o próprio catálogo).
CREATE TABLE IF NOT EXISTS filiais (
    id         INTEGER PRIMARY KEY,
    codigo     TEXT NOT NULL UNIQUE,
    nome       TEXT NOT NULL,
    regiao_id  INTEGER NOT NULL REFERENCES regioes(id),
    ativo      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS usuarios (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE,
    nome          TEXT NOT NULL,
    papel         TEXT NOT NULL CHECK (papel IN ('admin', 'gerente', 'assistente')),
    filial_id     INTEGER REFERENCES filiais(id),
    senha_hash    TEXT NOT NULL,
    trocar_senha  INTEGER NOT NULL DEFAULT 1,
    ativo         INTEGER NOT NULL DEFAULT 1,
    criado_em     TEXT NOT NULL,
    ultimo_login  TEXT
);

-- Todo arquivo enviado ou gerado fica guardado, com hash (prova de que não mudou depois).
CREATE TABLE IF NOT EXISTS arquivos (
    id             INTEGER PRIMARY KEY,
    tipo           TEXT NOT NULL,          -- tabela_gerente, export_mercos, importacao, relatorio
    nome_original  TEXT NOT NULL,
    caminho        TEXT NOT NULL,
    tamanho        INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    usuario_id     INTEGER REFERENCES usuarios(id),
    criado_em      TEXT NOT NULL,
    analise_json   TEXT                    -- abas, linhas ocultas, filtros, vínculos externos...
);

-- Tabela de preço publicada pelo gerente, por região e vigência.
CREATE TABLE IF NOT EXISTS tabelas_preco (
    id              INTEGER PRIMARY KEY,
    regiao_id       INTEGER NOT NULL REFERENCES regioes(id),
    vigencia        TEXT NOT NULL,         -- AAAA-MM-DD
    desconto_max    REAL,                  -- % máximo de desconto definido pelo gerente
    observacao      TEXT,
    arquivo_id      INTEGER NOT NULL REFERENCES arquivos(id),
    usuario_id      INTEGER NOT NULL REFERENCES usuarios(id),
    status          TEXT NOT NULL CHECK (status IN ('rascunho', 'publicada', 'substituida', 'descartada')),
    criado_em       TEXT NOT NULL,
    publicado_em    TEXT,
    total_itens     INTEGER NOT NULL DEFAULT 0,
    resumo_json     TEXT
);

CREATE TABLE IF NOT EXISTS tabela_itens (
    id         INTEGER PRIMARY KEY,
    tabela_id  INTEGER NOT NULL REFERENCES tabelas_preco(id),
    codigo     TEXT NOT NULL,
    nome       TEXT,
    embalagem  TEXT,
    cif        REAL,
    fob        REAL,
    linha      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tabela_itens ON tabela_itens(tabela_id, codigo);

-- Lote = uma importação de uma filial (do envio da exportação até a confirmação no Mercos).
CREATE TABLE IF NOT EXISTS lotes (
    id                    INTEGER PRIMARY KEY,
    filial_id             INTEGER NOT NULL REFERENCES filiais(id),
    tabela_id             INTEGER NOT NULL REFERENCES tabelas_preco(id),
    usuario_id            INTEGER NOT NULL REFERENCES usuarios(id),
    status                TEXT NOT NULL CHECK (status IN ('analisado', 'gerado', 'importado', 'cancelado')),
    criado_em             TEXT NOT NULL,
    gerado_em             TEXT,
    importado_em          TEXT,
    cancelado_em          TEXT,
    arquivo_export_id     INTEGER REFERENCES arquivos(id),
    arquivo_saida_id      INTEGER REFERENCES arquivos(id),
    arquivo_relatorio_id  INTEGER REFERENCES arquivos(id),
    resumo_json           TEXT
);

CREATE TABLE IF NOT EXISTS lote_decisoes (
    id               INTEGER PRIMARY KEY,
    lote_id          INTEGER NOT NULL REFERENCES lotes(id),
    linha            INTEGER NOT NULL,
    codigo           TEXT,
    nome             TEXT,
    acao             TEXT NOT NULL,        -- manter, remover, preencher
    motivo           TEXT,
    grupo            TEXT,
    manual           INTEGER NOT NULL DEFAULT 0,
    codigo_sugerido  TEXT,
    escolhido_por    INTEGER REFERENCES usuarios(id)
);
CREATE INDEX IF NOT EXISTS ix_lote_decisoes ON lote_decisoes(lote_id);

CREATE TABLE IF NOT EXISTS lote_precos (
    id          INTEGER PRIMARY KEY,
    lote_id     INTEGER NOT NULL REFERENCES lotes(id),
    codigo      TEXT NOT NULL,
    nome        TEXT,
    cif_antes   REAL,
    cif_depois  REAL,
    fob_antes   REAL,
    fob_depois  REAL,
    min_depois  REAL,
    var_cif     REAL,                      -- variação % do CIF
    origem      TEXT NOT NULL,             -- tabela, mantido
    inativo     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_lote_precos ON lote_precos(lote_id);
CREATE INDEX IF NOT EXISTS ix_lote_precos_codigo ON lote_precos(codigo);

-- Foto do catálogo do Mercos de cada filial a cada exportação enviada.
CREATE TABLE IF NOT EXISTS catalogo_fotos (
    id              INTEGER PRIMARY KEY,
    filial_id       INTEGER NOT NULL REFERENCES filiais(id),
    lote_id         INTEGER REFERENCES lotes(id),
    criado_em       TEXT NOT NULL,
    total_linhas    INTEGER,
    com_codigo      INTEGER,
    sem_codigo      INTEGER,
    duplicados      INTEGER,
    inativos        INTEGER,
    ocultas         INTEGER,
    codigos_json    TEXT
);

-- Log de tudo: acessos, ações, erros e eventos do sistema.
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY,
    quando      TEXT NOT NULL,
    usuario_id  INTEGER,
    login       TEXT,
    ip          TEXT,
    navegador   TEXT,
    tipo        TEXT NOT NULL,             -- acesso, acao, login, erro, sistema
    acao        TEXT NOT NULL,
    metodo      TEXT,
    rota        TEXT,
    status      INTEGER,
    duracao_ms  INTEGER,
    sucesso     INTEGER NOT NULL DEFAULT 1,
    filial_id   INTEGER,
    lote_id     INTEGER,
    detalhes    TEXT
);
CREATE INDEX IF NOT EXISTS ix_eventos_quando ON eventos(quando);
CREATE INDEX IF NOT EXISTS ix_eventos_usuario ON eventos(usuario_id);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);
"""

CONFIG_PADRAO = {
    "variacao_destaque": "5",          # % de variação do CIF que aparece em destaque
    "regra_preco_minimo": "desconto",  # desconto = CIF x (1 - desconto máximo do gerente) | fob = FOB da tabela
    "retencao_anos": "5",              # tempo de guarda de logs e arquivos
}


def agora() -> str:
    # Formato "AAAA-MM-DD HH:MM:SS", o mesmo do datetime() do SQLite (permite comparar como texto).
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(config.BANCO, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


@contextmanager
def transacao():
    con = conectar()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def consultar(sql: str, params=()) -> list[sqlite3.Row]:
    con = conectar()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def consultar_um(sql: str, params=()):
    linhas = consultar(sql, params)
    return linhas[0] if linhas else None


def executar(sql: str, params=()) -> int:
    with transacao() as con:
        return con.execute(sql, params).lastrowid


def obter_config(chave: str) -> str:
    linha = consultar_um("SELECT valor FROM configuracoes WHERE chave = ?", (chave,))
    return linha["valor"] if linha else CONFIG_PADRAO[chave]


def inicializar() -> None:
    with transacao() as con:
        con.executescript(ESQUEMA)
        for chave, valor in CONFIG_PADRAO.items():
            con.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES (?, ?)", (chave, valor))
        # Regiões e filiais não vêm prontas: o administrador cadastra na tela "Filiais".


def para_json(dados) -> str:
    return json.dumps(dados, ensure_ascii=False, default=str)


def de_json(texto):
    return json.loads(texto) if texto else None
