"""App Comercial Interno — importação de tabelas de preço no Mercos, com log completo."""
import io
import secrets
import time
import traceback
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import openpyxl
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auditoria, config, db, planilhas
from .seguranca import (PrecisaLogin, conferir_csrf, conferir_senha, exigir, gerar_hash, ip_do_usuario,
                        senha_aceitavel, tentativas_falhas_recentes, token_csrf, usuario_atual)

PASTA_APP = Path(__file__).resolve().parent


@asynccontextmanager
async def ciclo_de_vida(_app):
    _ao_iniciar()
    yield
    auditoria.registrar("app_encerrado", tipo="sistema")


app = FastAPI(title="Comercial Interno", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=ciclo_de_vida)
app.mount("/static", StaticFiles(directory=PASTA_APP / "static"), name="static")
templates = Jinja2Templates(directory=PASTA_APP / "templates")

PAPEIS = {"admin": "Administrador (TI)", "gerente": "Gerente comercial", "assistente": "Assistente comercial"}
STATUS_LOTE = {"analisado": "Aguardando gerar", "gerado": "Gerado - falta importar no Mercos",
               "importado": "Importado no Mercos", "cancelado": "Cancelado"}
STATUS_TABELA = {"rascunho": "Rascunho", "publicada": "Publicada (vigente)", "substituida": "Substituída",
                 "descartada": "Descartada"}


def moeda(valor) -> str:
    if valor is None:
        return "—"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def porcentagem(valor) -> str:
    return "—" if valor is None else f"{valor:+.2f}%".replace(".", ",")


def data_br(texto) -> str:
    if not texto:
        return "—"
    texto = str(texto)
    if len(texto) >= 10 and texto[4] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[0:4]}{texto[10:16]}"
    return texto


templates.env.filters.update(moeda=moeda, porcentagem=porcentagem, data_br=data_br,
                             numero=lambda v: f"{v:g}".replace(".", ","))
templates.env.globals.update(PAPEIS=PAPEIS, STATUS_LOTE=STATUS_LOTE, STATUS_TABELA=STATUS_TABELA,
                             EMPRESA=config.EMPRESA)


# ------------------------------------------------------------------ apoio

def avisar(request: Request, texto: str, tipo: str = "ok") -> None:
    request.session.setdefault("mensagens", []).append([tipo, texto])


def pagina(request: Request, nome: str, status_code: int = 200, **contexto):
    contexto.update(
        usuario=usuario_atual(request),
        csrf=token_csrf(request),
        mensagens=request.session.pop("mensagens", []),
    )
    return templates.TemplateResponse(request, nome, contexto, status_code=status_code)


def ir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


async def ler_upload(arquivo: UploadFile) -> bytes:
    dados = await arquivo.read(config.TAMANHO_MAX_ARQUIVO + 1)
    if len(dados) > config.TAMANHO_MAX_ARQUIVO:
        raise planilhas.PlanilhaInvalida("Arquivo maior que 20 MB.")
    if not dados:
        raise planilhas.PlanilhaInvalida("Nenhum arquivo foi enviado.")
    if not (arquivo.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise planilhas.PlanilhaInvalida("Envie a planilha no formato .xlsx.")
    return dados


def tabela_vigente(regiao_id: int):
    return db.consultar_um(
        "SELECT t.*, r.codigo AS regiao_codigo, r.nome AS regiao_nome, u.nome AS usuario_nome "
        "FROM tabelas_preco t JOIN regioes r ON r.id = t.regiao_id JOIN usuarios u ON u.id = t.usuario_id "
        "WHERE t.regiao_id = ? AND t.status = 'publicada' ORDER BY t.publicado_em DESC LIMIT 1",
        (regiao_id,),
    )


def itens_da_tabela(tabela_id: int) -> dict[str, planilhas.ItemTabela]:
    return {
        l["codigo"]: planilhas.ItemTabela(l["codigo"], l["nome"] or "", l["embalagem"] or "", l["cif"], l["fob"], l["linha"])
        for l in db.consultar("SELECT * FROM tabela_itens WHERE tabela_id = ?", (tabela_id,))
    }


def filial_do_pedido(usuario, filial_id: int | None):
    """Assistente só trabalha na própria filial; admin escolhe."""
    if usuario["papel"] == "assistente":
        filial_id = usuario["filial_id"]
    if not filial_id:
        return None
    return db.consultar_um(
        "SELECT f.*, r.codigo AS regiao_codigo, r.nome AS regiao_nome FROM filiais f "
        "JOIN regioes r ON r.id = f.regiao_id WHERE f.id = ? AND f.ativo = 1", (filial_id,))


def carregar_lote(lote_id: int, usuario):
    lote = db.consultar_um(
        "SELECT l.*, f.codigo AS filial_codigo, f.nome AS filial_nome, u.nome AS usuario_nome, "
        "t.vigencia, t.desconto_max, r.codigo AS regiao_codigo "
        "FROM lotes l JOIN filiais f ON f.id = l.filial_id JOIN usuarios u ON u.id = l.usuario_id "
        "JOIN tabelas_preco t ON t.id = l.tabela_id JOIN regioes r ON r.id = t.regiao_id WHERE l.id = ?",
        (lote_id,),
    )
    if lote is None:
        raise HTTPException(404, "Importação não encontrada.")
    if usuario["papel"] == "assistente" and lote["filial_id"] != usuario["filial_id"]:
        raise HTTPException(403, "Esta importação é de outra filial.")
    if usuario["papel"] == "gerente":
        raise HTTPException(403, "Seu usuário não tem acesso às importações.")
    return lote


# --------------------------------------------------------- log de acessos

@app.middleware("http")
async def log_de_acesso(request: Request, call_next):
    inicio = time.perf_counter()
    resposta = await call_next(request)
    rota = request.url.path
    if not rota.startswith("/static") and rota != "/favicon.ico":
        try:
            usuario_id = request.session.get("usuario_id")
            usuario = {"id": usuario_id, "login": request.session.get("login")} if usuario_id else None
        except AssertionError:
            usuario = None
        auditoria.registrar(
            "acesso", tipo="acesso", request=request, usuario=usuario,
            status=resposta.status_code, duracao_ms=int((time.perf_counter() - inicio) * 1000),
            sucesso=resposta.status_code < 400,
        )
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "same-origin"
    if config.COOKIE_SEGURO:  # produção (HTTPS): o navegador passa a usar sempre HTTPS neste endereço
        resposta.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resposta


app.add_middleware(
    SessionMiddleware, secret_key=config.SEGREDO, session_cookie="comercial_sessao",
    max_age=10 * 60 * 60, same_site="lax", https_only=config.COOKIE_SEGURO,
)


@app.exception_handler(PrecisaLogin)
async def _precisa_login(request: Request, _erro):
    return ir("/login")


@app.exception_handler(HTTPException)
async def _erro_http(request: Request, erro: HTTPException):
    if erro.status_code in (403, 400):
        auditoria.registrar("negado" if erro.status_code == 403 else "requisicao_invalida", tipo="erro",
                            request=request, usuario=usuario_atual(request), sucesso=False,
                            detalhes={"mensagem": erro.detail})
    return pagina(request, "erro.html", status_code=erro.status_code, erro=erro.detail, codigo=erro.status_code)


@app.exception_handler(RequestValidationError)
async def _parametro_invalido(request: Request, erro: RequestValidationError):
    auditoria.registrar("parametro_invalido", tipo="erro", request=request, usuario=usuario_atual(request),
                        sucesso=False, detalhes={"erros": [str(e.get("msg")) + " " + str(e.get("loc")) for e in erro.errors()]})
    return pagina(request, "erro.html", status_code=400, codigo=400,
                  erro="Algum campo da tela veio vazio ou em formato inválido. Volte e tente de novo.")


@app.exception_handler(Exception)
async def _erro_geral(request: Request, erro: Exception):
    # Este tratador roda fora do middleware de sessão: não dá para ler o usuário pela sessão aqui.
    auditoria.registrar("erro_interno", tipo="erro", request=request, sucesso=False,
                        detalhes={"erro": repr(erro), "rastro": traceback.format_exc()[-4000:]})
    return templates.TemplateResponse(
        request, "erro.html", {"usuario": None, "csrf": "", "mensagens": [], "codigo": 500,
                               "erro": "Erro inesperado. O TI já recebeu o registro deste erro."}, status_code=500)


def _ao_iniciar():
    db.inicializar()
    if not db.consultar_um("SELECT id FROM usuarios LIMIT 1"):
        senha = secrets.token_urlsafe(12)
        db.executar(
            "INSERT INTO usuarios (login, nome, papel, senha_hash, trocar_senha, criado_em) VALUES (?,?,?,?,1,?)",
            ("admin", "Administrador", "admin", gerar_hash(senha), db.agora()),
        )
        (config.PASTA_DADOS / "SENHA_INICIAL_ADMIN.txt").write_text(
            f"Usuário: admin\nSenha temporária: {senha}\nApague este arquivo depois do primeiro login.\n",
            encoding="utf-8")
        print(f"\n*** Usuário 'admin' criado. Senha temporária em {config.PASTA_DADOS / 'SENHA_INICIAL_ADMIN.txt'}\n")
    auditoria.registrar("app_iniciado", tipo="sistema",
                        detalhes={"segredo_fixo": config.SEGREDO_FIXO, "cookie_seguro": config.COOKIE_SEGURO})


# ------------------------------------------------------------------ login

@app.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    if usuario_atual(request):
        return ir("/")
    return pagina(request, "login.html")


@app.post("/login")
def fazer_login(request: Request, login: str = Form(...), senha: str = Form(...), csrf: str = Form("")):
    try:
        conferir_csrf(request, csrf)
    except HTTPException:
        # tela de login aberta antes de o servidor reiniciar: só pede para tentar de novo
        avisar(request, "A página de login tinha expirado. Digite usuário e senha de novo.", "alerta")
        return ir("/login")
    ip = ip_do_usuario(request)
    login = login.strip().lower()
    if tentativas_falhas_recentes(ip) >= 10:
        auditoria.registrar("login", tipo="login", request=request, login=login, sucesso=False,
                            detalhes={"motivo": "bloqueado por excesso de tentativas"})
        avisar(request, "Muitas tentativas erradas. Aguarde 15 minutos.", "erro")
        return ir("/login")
    usuario = db.consultar_um("SELECT * FROM usuarios WHERE login = ?", (login,))
    if usuario is None or not usuario["ativo"] or not conferir_senha(senha, usuario["senha_hash"]):
        motivo = "usuário inexistente" if usuario is None else ("usuário desativado" if not usuario["ativo"] else "senha errada")
        auditoria.registrar("login", tipo="login", request=request, login=login, sucesso=False, detalhes={"motivo": motivo})
        avisar(request, "Usuário ou senha incorretos.", "erro")
        return ir("/login")
    request.session.clear()
    request.session.update(usuario_id=usuario["id"], login=usuario["login"])
    db.executar("UPDATE usuarios SET ultimo_login = ? WHERE id = ?", (db.agora(), usuario["id"]))
    auditoria.registrar("login", tipo="login", request=request, usuario=usuario)
    return ir("/senha" if usuario["trocar_senha"] else "/")


@app.post("/logout")
def sair(request: Request, csrf: str = Form("")):
    conferir_csrf(request, csrf)
    auditoria.registrar("logout", tipo="login", request=request, usuario=usuario_atual(request))
    request.session.clear()
    return ir("/login")


@app.get("/senha", response_class=HTMLResponse)
def tela_senha(request: Request):
    exigir(request)
    return pagina(request, "senha.html")


@app.post("/senha")
def trocar_senha(request: Request, atual: str = Form(...), nova: str = Form(...), confirmacao: str = Form(...),
                 csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request)
    if not conferir_senha(atual, usuario["senha_hash"]):
        auditoria.registrar("trocar_senha", request=request, usuario=usuario, sucesso=False, detalhes={"motivo": "senha atual errada"})
        avisar(request, "A senha atual está errada.", "erro")
        return ir("/senha")
    problema = senha_aceitavel(nova) or ("A confirmação não confere com a nova senha." if nova != confirmacao else None)
    if problema:
        avisar(request, problema, "erro")
        return ir("/senha")
    db.executar("UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?", (gerar_hash(nova), usuario["id"]))
    auditoria.registrar("trocar_senha", request=request, usuario=usuario)
    avisar(request, "Senha alterada.")
    return ir("/")


# ----------------------------------------------------------------- início

@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    usuario = exigir(request)
    if usuario["trocar_senha"]:
        return ir("/senha")
    filiais = db.consultar(
        "SELECT f.*, r.codigo AS regiao_codigo FROM filiais f JOIN regioes r ON r.id = f.regiao_id "
        "WHERE f.ativo = 1 ORDER BY f.id")
    situacao = []
    for filial in filiais:
        if usuario["papel"] == "assistente" and filial["id"] != usuario["filial_id"]:
            continue
        vigente = tabela_vigente(filial["regiao_id"])
        ultimo = db.consultar_um(
            "SELECT l.*, u.nome AS usuario_nome FROM lotes l JOIN usuarios u ON u.id = l.usuario_id "
            "WHERE l.filial_id = ? AND l.status = 'importado' ORDER BY l.importado_em DESC LIMIT 1", (filial["id"],))
        situacao.append({
            "filial": filial, "vigente": vigente, "ultimo": ultimo,
            "atualizada": bool(vigente and ultimo and ultimo["tabela_id"] == vigente["id"]),
        })
    alertas = []
    if usuario["papel"] == "admin":
        for lote in db.consultar(
                "SELECT l.id, f.nome AS filial, u.nome AS usuario, l.gerado_em FROM lotes l "
                "JOIN filiais f ON f.id = l.filial_id JOIN usuarios u ON u.id = l.usuario_id "
                "WHERE l.status = 'gerado' AND l.gerado_em <= datetime('now', 'localtime', '-1 day')"):
            alertas.append(f"Importação #{lote['id']} ({lote['filial']}, {lote['usuario']}) foi gerada em "
                           f"{data_br(lote['gerado_em'])} e ainda não foi confirmada no Mercos.")
        for s in situacao:
            if s["vigente"] and not s["atualizada"]:
                alertas.append(f"{s['filial']['nome']} ainda não importou a tabela vigente "
                               f"({s['vigente']['regiao_codigo']}, vigência {data_br(s['vigente']['vigencia'])}).")
        falhas = db.consultar_um("SELECT COUNT(*) AS n FROM eventos WHERE tipo IN ('erro', 'login') AND sucesso = 0 "
                                 "AND quando >= datetime('now', 'localtime', '-1 day')")["n"]
        if falhas:
            alertas.append(f"{falhas} eventos com falha (erros ou logins recusados) nas últimas 24 horas.")
    rascunhos = db.consultar("SELECT t.id, r.codigo AS regiao, t.vigencia FROM tabelas_preco t "
                             "JOIN regioes r ON r.id = t.regiao_id WHERE t.status = 'rascunho'")
    return pagina(request, "inicio.html", situacao=situacao, alertas=alertas, rascunhos=rascunhos)


# ------------------------------------------------- tabelas do gerente

@app.get("/tabelas", response_class=HTMLResponse)
def tabelas(request: Request):
    exigir(request, "gerente")
    regioes = db.consultar("SELECT * FROM regioes ORDER BY id")
    lista = db.consultar(
        "SELECT t.*, r.codigo AS regiao_codigo, u.nome AS usuario_nome FROM tabelas_preco t "
        "JOIN regioes r ON r.id = t.regiao_id JOIN usuarios u ON u.id = t.usuario_id "
        "ORDER BY t.criado_em DESC LIMIT 100")
    return pagina(request, "tabelas.html", regioes=regioes, lista=lista, hoje=date.today().isoformat())


@app.post("/tabelas/enviar")
async def enviar_tabela(request: Request, regiao_id: str = Form(""), vigencia: str = Form(...),
                        desconto_max: str = Form(""), observacao: str = Form(""),
                        arquivo: UploadFile = File(...), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "gerente")
    regiao_id = inteiro_ou_nada(regiao_id)
    regiao = db.consultar_um("SELECT * FROM regioes WHERE id = ?", (regiao_id,)) if regiao_id else None
    if regiao is None:
        avisar(request, "Escolha a região da tabela (ela define quais filiais recebem os preços).", "erro")
        return ir("/tabelas")
    desconto = planilhas.numero(desconto_max) if desconto_max.strip() else None
    if desconto is None or not 0 <= desconto < 100:
        avisar(request, "Informe o desconto máximo do vendedor (entre 0 e 99%). Ele define o Preço Mínimo no Mercos.", "erro")
        return ir("/tabelas")
    try:
        date.fromisoformat(vigencia)
        dados = await ler_upload(arquivo)
        lida = planilhas.ler_tabela_gerente(dados)
    except (planilhas.PlanilhaInvalida, ValueError) as erro:
        auditoria.registrar("tabela_enviada", request=request, usuario=usuario, sucesso=False,
                            detalhes={"arquivo": arquivo.filename, "erro": str(erro)})
        avisar(request, f"Tabela recusada: {erro}", "erro")
        return ir("/tabelas")

    arquivo_id = auditoria.guardar_arquivo(dados, arquivo.filename, "tabela_gerente", usuario["id"], lida.analise)
    anterior = tabela_vigente(regiao_id)
    itens_anteriores = itens_da_tabela(anterior["id"]) if anterior else {}
    variacoes = []
    for codigo, item in lida.itens.items():
        velho = itens_anteriores.get(codigo)
        if velho and velho.cif and item.cif and (velho.cif, velho.fob) != (item.cif, item.fob):
            variacoes.append({"codigo": codigo, "nome": f"{item.nome} {item.embalagem}".strip(),
                              "cif_antes": velho.cif, "cif": item.cif, "fob_antes": velho.fob, "fob": item.fob,
                              "var": round((item.cif - velho.cif) / velho.cif * 100, 2)})
    variacoes.sort(key=lambda v: abs(v["var"]), reverse=True)
    resumo = {
        "aba": lida.aba, "linha_cabecalho": lida.linha_cabecalho, "colunas": lida.colunas,
        "conflitos": lida.conflitos, "repetidos_iguais": lida.repetidos_iguais, "sem_preco": lida.sem_preco,
        "tabela_anterior_id": anterior["id"] if anterior else None,
        "novos": sorted(set(lida.itens) - set(itens_anteriores)) if anterior else [],
        "retirados": sorted(set(itens_anteriores) - set(lida.itens)) if anterior else [],
        "variacoes": variacoes,
    }
    with db.transacao() as con:
        tabela_id = con.execute(
            "INSERT INTO tabelas_preco (regiao_id, vigencia, desconto_max, observacao, arquivo_id, usuario_id, status, "
            "criado_em, total_itens, resumo_json) VALUES (?,?,?,?,?,?, 'rascunho', ?,?,?)",
            (regiao_id, vigencia, desconto, observacao.strip() or None, arquivo_id, usuario["id"], db.agora(),
             len(lida.itens), db.para_json(resumo)),
        ).lastrowid
        con.executemany(
            "INSERT INTO tabela_itens (tabela_id, codigo, nome, embalagem, cif, fob, linha) VALUES (?,?,?,?,?,?,?)",
            [(tabela_id, i.codigo, i.nome, i.embalagem, i.cif, i.fob, i.linha) for i in lida.itens.values()],
        )
    auditoria.registrar("tabela_enviada", request=request, usuario=usuario, detalhes={
        "tabela_id": tabela_id, "regiao": regiao["codigo"], "vigencia": vigencia, "desconto_max": desconto,
        "arquivo": arquivo.filename, "itens": len(lida.itens), "conflitos": len(lida.conflitos),
        "variacoes": len(variacoes)})
    return ir(f"/tabelas/{tabela_id}")


@app.get("/tabelas/{tabela_id}", response_class=HTMLResponse)
def ver_tabela(request: Request, tabela_id: int):
    exigir(request, "gerente")
    tabela = db.consultar_um(
        "SELECT t.*, r.codigo AS regiao_codigo, r.nome AS regiao_nome, u.nome AS usuario_nome, a.nome_original, "
        "a.sha256, a.analise_json FROM tabelas_preco t JOIN regioes r ON r.id = t.regiao_id "
        "JOIN usuarios u ON u.id = t.usuario_id JOIN arquivos a ON a.id = t.arquivo_id WHERE t.id = ?", (tabela_id,))
    if tabela is None:
        raise HTTPException(404, "Tabela não encontrada.")
    destaque = float(db.obter_config("variacao_destaque"))
    filiais_da_regiao = db.consultar(
        "SELECT codigo, nome FROM filiais WHERE regiao_id = ? AND ativo = 1 ORDER BY nome", (tabela["regiao_id"],))
    return pagina(request, "tabela.html", tabela=tabela, resumo=db.de_json(tabela["resumo_json"]),
                  analise=db.de_json(tabela["analise_json"]), destaque=destaque, filiais_da_regiao=filiais_da_regiao)


@app.post("/tabelas/{tabela_id}/publicar")
def publicar_tabela(request: Request, tabela_id: int, ignorar_conflitos: str = Form(""),
                    confirmo_filiais: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "gerente")
    tabela = db.consultar_um("SELECT * FROM tabelas_preco WHERE id = ?", (tabela_id,))
    if tabela is None or tabela["status"] != "rascunho":
        raise HTTPException(400, "Só dá para publicar uma tabela em rascunho.")
    if confirmo_filiais != "1":
        avisar(request, "Marque a confirmação das filiais que vão receber esta tabela.", "erro")
        return ir(f"/tabelas/{tabela_id}")
    filiais_alvo = [f"{f['nome']} ({f['codigo']})" for f in db.consultar(
        "SELECT codigo, nome FROM filiais WHERE regiao_id = ? AND ativo = 1 ORDER BY nome", (tabela["regiao_id"],))]
    resumo = db.de_json(tabela["resumo_json"])
    if resumo["conflitos"] and ignorar_conflitos != "1":
        avisar(request, "Há códigos repetidos com preços diferentes. Corrija a planilha ou marque a confirmação.", "erro")
        return ir(f"/tabelas/{tabela_id}")
    with db.transacao() as con:
        con.execute("UPDATE tabelas_preco SET status = 'substituida' WHERE regiao_id = ? AND status = 'publicada'",
                    (tabela["regiao_id"],))
        con.execute("UPDATE tabelas_preco SET status = 'publicada', publicado_em = ? WHERE id = ?", (db.agora(), tabela_id))
    auditoria.registrar("tabela_publicada", request=request, usuario=usuario,
                        detalhes={"tabela_id": tabela_id, "regiao_id": tabela["regiao_id"], "vigencia": tabela["vigencia"],
                                  "filiais_confirmadas": filiais_alvo,
                                  "codigos_ignorados_por_conflito": [c["codigo"] for c in resumo["conflitos"]]})
    avisar(request, "Tabela publicada. As assistentes já podem gerar as importações.")
    return ir(f"/tabelas/{tabela_id}")


@app.post("/tabelas/{tabela_id}/descartar")
def descartar_tabela(request: Request, tabela_id: int, csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "gerente")
    db.executar("UPDATE tabelas_preco SET status = 'descartada' WHERE id = ? AND status = 'rascunho'", (tabela_id,))
    auditoria.registrar("tabela_descartada", request=request, usuario=usuario, detalhes={"tabela_id": tabela_id})
    avisar(request, "Rascunho descartado.")
    return ir("/tabelas")


# ---------------------------------------------------------- importação

def inteiro_ou_nada(texto: str | None) -> int | None:
    """Filtros de tela mandam '' quando nada é escolhido; trata isso como 'sem filtro'."""
    texto = (texto or "").strip()
    return int(texto) if texto.isdigit() else None


@app.get("/importacao", response_class=HTMLResponse)
def importacao(request: Request, filial: str | None = None):
    usuario = exigir(request, "assistente")
    filiais = db.consultar("SELECT * FROM filiais WHERE ativo = 1 ORDER BY id")
    escolhida = filial_do_pedido(usuario, inteiro_ou_nada(filial))
    vigente = tabela_vigente(escolhida["regiao_id"]) if escolhida else None
    lotes = db.consultar(
        "SELECT l.*, u.nome AS usuario_nome, t.vigencia FROM lotes l JOIN usuarios u ON u.id = l.usuario_id "
        "JOIN tabelas_preco t ON t.id = l.tabela_id WHERE l.filial_id = ? ORDER BY l.criado_em DESC LIMIT 20",
        (escolhida["id"] if escolhida else 0,))
    return pagina(request, "importacao.html", filiais=filiais, filial=escolhida, vigente=vigente, lotes=lotes)


def _numeros_previa(saida: list, destaque: float) -> dict:
    """saida = todos os produtos válidos; só os 'alterado' vão no arquivo."""
    enviar = planilhas.somente_alterados(saida)
    return {
        "linhas_saida": len(enviar),
        "sem_alteracao": len(saida) - len(enviar),
        "precos_alterados": sum(1 for s in enviar if s.origem == "tabela"),
        "em_destaque": sum(1 for s in enviar if s.var_cif is not None and abs(s.var_cif) >= destaque),
        "mantidos_fora_tabela": sum(1 for s in saida if s.origem == "mantido"),
        "inativos": sum(1 for s in enviar if s.inativo),
    }


def _gravar_precos(con, lote_id: int, saida: list) -> None:
    saida = planilhas.somente_alterados(saida)
    con.execute("DELETE FROM lote_precos WHERE lote_id = ?", (lote_id,))
    con.executemany(
        "INSERT INTO lote_precos (lote_id, codigo, nome, cif_antes, cif_depois, fob_antes, fob_depois, min_depois, "
        "var_cif, origem, inativo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(lote_id, s.codigo, s.nome, s.cif_antes, s.cif_depois, s.fob_antes, s.fob_depois, s.preco_minimo,
          s.var_cif, s.origem, int(s.inativo)) for s in saida],
    )


def _semelhanca_catalogo(codigos: set[str], filial_id: int) -> dict:
    """Compara os códigos com a última foto de cada filial (detecta exportação da filial errada)."""
    resultado = {}
    for foto in db.consultar(
            "SELECT c.filial_id, f.nome, c.codigos_json FROM catalogo_fotos c JOIN filiais f ON f.id = c.filial_id "
            "WHERE c.id IN (SELECT MAX(id) FROM catalogo_fotos GROUP BY filial_id)"):
        anteriores = set(db.de_json(foto["codigos_json"]) or [])
        if anteriores and codigos:
            resultado[foto["filial_id"]] = {"nome": foto["nome"],
                                            "igualdade": round(len(codigos & anteriores) / len(codigos | anteriores), 3)}
    propria = resultado.get(filial_id, {}).get("igualdade")
    outra = max((v for k, v in resultado.items() if k != filial_id), key=lambda v: v["igualdade"], default=None)
    suspeita = bool(propria is not None and outra and outra["igualdade"] > propria + 0.05 and propria < 0.9)
    return {"comparacao": resultado, "suspeita": suspeita, "parece": outra["nome"] if suspeita else None}


@app.post("/importacao/enviar")
async def enviar_exportacao(request: Request, filial_id: int = Form(0), arquivo: UploadFile = File(...),
                            csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "assistente")
    filial = filial_do_pedido(usuario, filial_id)
    if filial is None:
        raise HTTPException(400, "Escolha a filial.")
    vigente = tabela_vigente(filial["regiao_id"])
    if vigente is None:
        avisar(request, "Ainda não há tabela publicada pelo gerente para esta região.", "erro")
        return ir(f"/importacao?filial={filial['id']}")
    try:
        dados = await ler_upload(arquivo)
        exportacao = planilhas.ler_exportacao_mercos(dados)
    except planilhas.PlanilhaInvalida as erro:
        auditoria.registrar("exportacao_enviada", request=request, usuario=usuario, sucesso=False, filial_id=filial["id"],
                            detalhes={"arquivo": arquivo.filename, "erro": str(erro)})
        avisar(request, f"Arquivo recusado: {erro}", "erro")
        return ir(f"/importacao?filial={filial['id']}")

    foto = planilhas.foto_catalogo(exportacao)
    conferencia = _semelhanca_catalogo(set(foto["codigos"]), filial["id"])
    tabela = itens_da_tabela(vigente["id"])
    plano = planilhas.montar_plano(exportacao, tabela)
    previa = planilhas.aplicar_precos(exportacao, plano.decisoes, tabela, db.obter_config("regra_preco_minimo"),
                                      vigente["desconto_max"])
    codigos_saida = set(foto["codigos"])  # tudo que existe no Mercos da filial (inclusive duplicados)
    destaque = float(db.obter_config("variacao_destaque"))
    resumo = {
        "arquivo": arquivo.filename,
        "catalogo": {k: v for k, v in foto.items() if k != "codigos"},
        "conferencia_filial": conferencia,
        "analise": exportacao.analise,
        "colunas": {k: planilhas.get_column_letter(v) for k, v in exportacao.colunas.items()},
        "avisos": plano.avisos,
        "cadastrar": [{"codigo": c, "nome": tabela[c].nome, "embalagem": tabela[c].embalagem,
                       "cif": tabela[c].cif, "fob": tabela[c].fob} for c in sorted(set(tabela) - codigos_saida)],
        "previa": _numeros_previa(previa, destaque),
    }
    arquivo_id = auditoria.guardar_arquivo(dados, arquivo.filename, "export_mercos", usuario["id"], exportacao.analise)
    with db.transacao() as con:
        lote_id = con.execute(
            "INSERT INTO lotes (filial_id, tabela_id, usuario_id, status, criado_em, arquivo_export_id, resumo_json) "
            "VALUES (?,?,?, 'analisado', ?,?,?)",
            (filial["id"], vigente["id"], usuario["id"], db.agora(), arquivo_id, db.para_json(resumo)),
        ).lastrowid
        con.executemany(
            "INSERT INTO lote_decisoes (lote_id, linha, codigo, nome, acao, motivo, grupo, manual, codigo_sugerido) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(lote_id, d.linha, d.codigo, d.nome, d.acao, d.motivo, d.grupo, int(d.manual), d.codigo_sugerido)
             for d in plano.decisoes],
        )
        _gravar_precos(con, lote_id, previa)
        con.execute(
            "INSERT INTO catalogo_fotos (filial_id, lote_id, criado_em, total_linhas, com_codigo, sem_codigo, duplicados, "
            "inativos, ocultas, codigos_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filial["id"], lote_id, db.agora(), foto["total_linhas"], foto["com_codigo"], foto["sem_codigo"],
             foto["duplicados"], foto["inativos"], foto["ocultas"], db.para_json(foto["codigos"])),
        )
    auditoria.registrar("exportacao_enviada", request=request, usuario=usuario, filial_id=filial["id"], lote_id=lote_id,
                        detalhes={"arquivo": arquivo.filename, "catalogo": resumo["catalogo"], "previa": resumo["previa"],
                                  "suspeita_filial_errada": conferencia["suspeita"]})
    return ir(f"/lotes/{lote_id}")


@app.get("/lotes", response_class=HTMLResponse)
def lista_lotes(request: Request, filial: str | None = None, status: str | None = None):
    usuario = exigir(request, "assistente")
    filial = inteiro_ou_nada(filial)
    status = status if status in STATUS_LOTE else None
    filtros, params = [], []
    if usuario["papel"] == "assistente":
        filtros.append("l.filial_id = ?")
        params.append(usuario["filial_id"])
    elif filial:
        filtros.append("l.filial_id = ?")
        params.append(filial)
    if status:
        filtros.append("l.status = ?")
        params.append(status)
    onde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
    lotes = db.consultar(
        "SELECT l.*, f.nome AS filial_nome, u.nome AS usuario_nome, t.vigencia, r.codigo AS regiao_codigo "
        "FROM lotes l JOIN filiais f ON f.id = l.filial_id JOIN usuarios u ON u.id = l.usuario_id "
        f"JOIN tabelas_preco t ON t.id = l.tabela_id JOIN regioes r ON r.id = t.regiao_id {onde} "
        "ORDER BY l.criado_em DESC LIMIT 300", params)
    filiais = db.consultar("SELECT * FROM filiais ORDER BY id")
    return pagina(request, "lotes.html", lotes=lotes, filiais=filiais, filtro_filial=filial, filtro_status=status)


@app.get("/lotes/{lote_id}", response_class=HTMLResponse)
def ver_lote(request: Request, lote_id: int):
    usuario = exigir(request, "assistente")
    lote = carregar_lote(lote_id, usuario)
    decisoes = db.consultar("SELECT * FROM lote_decisoes WHERE lote_id = ? ORDER BY linha", (lote_id,))
    sem_codigo = {}
    for d in decisoes:
        if d["grupo"] == "sem_codigo" and d["acao"] == "remover" and not d["manual"]:
            sem_codigo[d["nome"]] = sem_codigo.get(d["nome"], 0) + 1
    duplicados = {}
    for d in decisoes:
        if d["grupo"] == "duplicado":
            duplicados.setdefault(d["codigo"], []).append(d)
    precos = db.consultar(
        "SELECT * FROM lote_precos WHERE lote_id = ? AND (var_cif IS NULL OR var_cif <> 0 OR fob_antes IS NOT fob_depois) "
        "AND origem = 'tabela' ORDER BY ABS(COALESCE(var_cif, 0)) DESC LIMIT 500", (lote_id,))
    arquivos = {
        tipo: db.consultar_um("SELECT * FROM arquivos WHERE id = ?", (lote[campo],)) if lote[campo] else None
        for tipo, campo in (("export", "arquivo_export_id"), ("saida", "arquivo_saida_id"), ("relatorio", "arquivo_relatorio_id"))
    }
    return pagina(request, "lote.html", lote=lote, resumo=db.de_json(lote["resumo_json"]),
                  sem_codigo=sorted(sem_codigo.items(), key=lambda x: -x[1]), duplicados=duplicados,
                  manuais=[d for d in decisoes if d["manual"]], precos=precos, arquivos=arquivos,
                  destaque=float(db.obter_config("variacao_destaque")))


@app.post("/lotes/{lote_id}/gerar")
async def gerar_lote(request: Request, lote_id: int):
    formulario = await request.form()
    conferir_csrf(request, formulario.get("csrf"))
    usuario = exigir(request, "assistente")
    lote = carregar_lote(lote_id, usuario)
    if lote["status"] != "analisado":
        raise HTTPException(400, "Esta importação já foi gerada ou cancelada.")
    resumo = db.de_json(lote["resumo_json"])
    if resumo["conferencia_filial"]["suspeita"] and formulario.get("confirmo_filial") != "1":
        avisar(request, "Confirme que a exportação é mesmo desta filial (caixa de confirmação).", "erro")
        return ir(f"/lotes/{lote_id}")

    decisoes = db.consultar("SELECT * FROM lote_decisoes WHERE lote_id = ? ORDER BY linha", (lote_id,))
    escolhas = []
    with db.transacao() as con:
        for d in decisoes:
            if not d["manual"]:
                continue
            if d["grupo"] == "duplicado":
                fica = formulario.get(f"dup_{d['codigo']}")
                if fica is None:
                    raise HTTPException(400, f"Escolha qual cadastro do código {d['codigo']} deve ficar.")
                acao = "manter" if str(d["linha"]) == fica else "remover"
            else:
                acao = "preencher" if formulario.get(f"preencher_{d['id']}") == "1" else "remover"
            con.execute("UPDATE lote_decisoes SET acao = ?, escolhido_por = ? WHERE id = ?", (acao, usuario["id"], d["id"]))
            escolhas.append({"linha": d["linha"], "codigo": d["codigo"] or d["codigo_sugerido"], "acao": acao})

    decisoes = [planilhas.Decisao(d["linha"], d["codigo"] or "", d["nome"] or "", d["acao"], d["motivo"] or "",
                                  d["grupo"] or "", bool(d["manual"]), d["codigo_sugerido"])
                for d in db.consultar("SELECT * FROM lote_decisoes WHERE lote_id = ? ORDER BY linha", (lote_id,))]
    arquivo_export = db.consultar_um("SELECT * FROM arquivos WHERE id = ?", (lote["arquivo_export_id"],))
    exportacao = planilhas.ler_exportacao_mercos(auditoria.caminho_arquivo(arquivo_export).read_bytes())
    tabela = itens_da_tabela(lote["tabela_id"])
    saida = planilhas.aplicar_precos(exportacao, decisoes, tabela, db.obter_config("regra_preco_minimo"), lote["desconto_max"])
    enviar = planilhas.somente_alterados(saida)
    codigos = [s.codigo for s in enviar]
    repetidos_no_mercos = {d.codigo for d in decisoes if d.grupo == "duplicado"}
    if (len(codigos) != len(set(codigos)) or any(not c for c in codigos)
            or repetidos_no_mercos & set(codigos)):
        raise HTTPException(400, "Conferência final falhou: há código repetido, vazio ou duplicado no Mercos. Avise o TI.")
    if not enviar:
        raise HTTPException(400, "Nenhum preço muda nesta filial: não há o que importar.")

    destaque = float(db.obter_config("variacao_destaque"))
    plano = planilhas.Plano(decisoes, resumo["avisos"])
    resumo_relatorio = {
        "Importação": f"#{lote_id}", "Filial": lote["filial_nome"], "Responsável": usuario["nome"],
        "Gerado em": data_br(db.agora()), "Tabela do gerente (vigência)": data_br(lote["vigencia"]),
        "Região": lote["regiao_codigo"], "Arquivo do Mercos": arquivo_export["nome_original"],
        "Produtos no arquivo do Mercos": resumo["catalogo"]["total_linhas"],
        "Linhas sem código (fora do arquivo)": sum(1 for d in decisoes if d.grupo == "sem_codigo"),
        "Códigos duplicados no Mercos (fora do arquivo)": len(repetidos_no_mercos),
        "Produtos sem alteração (fora do arquivo)": len(saida) - len(enviar),
        "Produtos no arquivo de importação": len(enviar),
        "Preços alterados da tabela do gerente": sum(1 for s in enviar if s.origem == "tabela"),
        f"Variação acima de {destaque:g}%": sum(1 for s in enviar if s.var_cif is not None and abs(s.var_cif) >= destaque),
        "Vão como inativos (preço zerado)": sum(1 for s in enviar if s.inativo),
        "Na tabela mas não existem nesta filial": len(resumo["cadastrar"]),
        "Estoque": "em branco (o Mercos mantém o atual; quem atualiza é o connector)",
        "NCM": "mantido como está no Mercos",
        "Conferir no Mercos (passo 3)": f"0 novos, {len(enviar)} atualizados, 0 substituídos, 0 excluídos",
    }
    dados_saida = planilhas.gerar_importacao_xlsx(exportacao, enviar)
    dados_relatorio = planilhas.gerar_relatorio_xlsx(resumo_relatorio, plano, saida, resumo["cadastrar"], destaque)
    base = f"{lote['filial_codigo']}_{lote['vigencia']}_lote{lote_id}"
    saida_id = auditoria.guardar_arquivo(dados_saida, f"IMPORTAR_MERCOS_{base}.xlsx", "importacao", usuario["id"])
    relatorio_id = auditoria.guardar_arquivo(dados_relatorio, f"RELATORIO_{base}.xlsx", "relatorio", usuario["id"])
    with db.transacao() as con:
        _gravar_precos(con, lote_id, saida)
        resumo["previa"] = _numeros_previa(saida, destaque)
        resumo["final"] = {k: v for k, v in resumo_relatorio.items()}
        resumo["escolhas_manuais"] = escolhas
        con.execute(
            "UPDATE lotes SET status = 'gerado', gerado_em = ?, arquivo_saida_id = ?, arquivo_relatorio_id = ?, "
            "resumo_json = ? WHERE id = ?",
            (db.agora(), saida_id, relatorio_id, db.para_json(resumo), lote_id))
    auditoria.registrar("importacao_gerada", request=request, usuario=usuario, filial_id=lote["filial_id"], lote_id=lote_id,
                        detalhes={"resumo": resumo_relatorio, "escolhas_manuais": escolhas,
                                  "confirmou_filial_suspeita": formulario.get("confirmo_filial") == "1"})
    avisar(request, "Arquivo gerado. Baixe, importe no Mercos da filial e depois confirme aqui.")
    return ir(f"/lotes/{lote_id}")


@app.post("/lotes/{lote_id}/importado")
def confirmar_importacao(request: Request, lote_id: int, confirmo: str = Form(""), passo3: str = Form(""),
                         observacao: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "assistente")
    lote = carregar_lote(lote_id, usuario)
    if lote["status"] != "gerado":
        raise HTTPException(400, "Só dá para confirmar uma importação já gerada.")
    if confirmo != "1" or passo3 != "1":
        avisar(request, "Marque as duas caixas: o resumo do passo 3 conferido e a importação concluída no Mercos.", "erro")
        return ir(f"/lotes/{lote_id}")
    esperado = (db.de_json(lote["resumo_json"]) or {}).get("previa", {}).get("linhas_saida")
    db.executar("UPDATE lotes SET status = 'importado', importado_em = ? WHERE id = ?", (db.agora(), lote_id))
    auditoria.registrar("importacao_confirmada", request=request, usuario=usuario, filial_id=lote["filial_id"],
                        lote_id=lote_id, detalhes={
                            "observacao": observacao.strip() or None,
                            "passo3_conferido": f"0 novos, {esperado} atualizados, 0 substituídos, 0 excluídos",
                        })
    avisar(request, "Importação registrada. Obrigado!")
    return ir(f"/lotes/{lote_id}")


@app.post("/lotes/{lote_id}/cancelar")
def cancelar_lote(request: Request, lote_id: int, motivo: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    usuario = exigir(request, "assistente")
    lote = carregar_lote(lote_id, usuario)
    if lote["status"] not in ("analisado", "gerado"):
        raise HTTPException(400, "Esta importação não pode mais ser cancelada.")
    db.executar("UPDATE lotes SET status = 'cancelado', cancelado_em = ? WHERE id = ?", (db.agora(), lote_id))
    auditoria.registrar("importacao_cancelada", request=request, usuario=usuario, filial_id=lote["filial_id"],
                        lote_id=lote_id, detalhes={"motivo": motivo.strip() or None, "status_anterior": lote["status"]})
    avisar(request, "Importação cancelada.")
    return ir(f"/importacao?filial={lote['filial_id']}")


# ---------------------------------------------------------- downloads

@app.get("/arquivos/{arquivo_id}")
def baixar_arquivo(request: Request, arquivo_id: int):
    usuario = exigir(request)
    arquivo = db.consultar_um("SELECT * FROM arquivos WHERE id = ?", (arquivo_id,))
    if arquivo is None:
        raise HTTPException(404, "Arquivo não encontrado.")
    lote = db.consultar_um(
        "SELECT * FROM lotes WHERE ? IN (arquivo_export_id, arquivo_saida_id, arquivo_relatorio_id)", (arquivo_id,))
    permitido = usuario["papel"] == "admin"
    if usuario["papel"] == "gerente":
        permitido = arquivo["tipo"] == "tabela_gerente"
    if usuario["papel"] == "assistente":
        permitido = bool(lote and lote["filial_id"] == usuario["filial_id"])
    if not permitido:
        raise HTTPException(403, "Você não tem acesso a este arquivo.")
    caminho = auditoria.caminho_arquivo(arquivo)
    if not caminho.exists():
        raise HTTPException(404, "O arquivo não está mais no servidor (prazo de guarda vencido).")
    auditoria.registrar("arquivo_baixado", request=request, usuario=usuario, lote_id=lote["id"] if lote else None,
                        filial_id=lote["filial_id"] if lote else None,
                        detalhes={"arquivo_id": arquivo_id, "tipo": arquivo["tipo"], "nome": arquivo["nome_original"],
                                  "sha256": arquivo["sha256"]})
    return FileResponse(caminho, filename=arquivo["nome_original"],
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------- administração

@app.get("/admin/usuarios", response_class=HTMLResponse)
def usuarios(request: Request):
    exigir(request, "admin")
    lista = db.consultar("SELECT u.*, f.nome AS filial_nome FROM usuarios u LEFT JOIN filiais f ON f.id = u.filial_id "
                         "ORDER BY u.ativo DESC, u.nome")
    filiais = db.consultar("SELECT * FROM filiais WHERE ativo = 1 ORDER BY id")
    return pagina(request, "usuarios.html", lista=lista, filiais=filiais,
                  senha_gerada=request.session.pop("senha_gerada", None))


def _filial_para_papel(papel: str, filial_id: str):
    if papel not in PAPEIS:
        raise HTTPException(400, "Papel inválido.")
    if papel == "assistente":
        if not filial_id:
            raise HTTPException(400, "Assistente precisa de uma filial.")
        return int(filial_id)
    return None


@app.post("/admin/usuarios/criar")
def criar_usuario(request: Request, login: str = Form(...), nome: str = Form(...), papel: str = Form(...),
                  filial_id: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    login = login.strip().lower()
    if not login.replace(".", "").replace("_", "").isalnum():
        avisar(request, "Login só pode ter letras, números, ponto e sublinhado.", "erro")
        return ir("/admin/usuarios")
    if db.consultar_um("SELECT id FROM usuarios WHERE login = ?", (login,)):
        avisar(request, f"Já existe o login {login}.", "erro")
        return ir("/admin/usuarios")
    filial = _filial_para_papel(papel, filial_id)
    senha = secrets.token_urlsafe(9)
    novo_id = db.executar(
        "INSERT INTO usuarios (login, nome, papel, filial_id, senha_hash, trocar_senha, criado_em) VALUES (?,?,?,?,?,1,?)",
        (login, nome.strip(), papel, filial, gerar_hash(senha), db.agora()))
    auditoria.registrar("usuario_criado", request=request, usuario=admin,
                        detalhes={"usuario_id": novo_id, "login": login, "papel": papel, "filial_id": filial})
    request.session["senha_gerada"] = [login, senha]
    return ir("/admin/usuarios")


@app.post("/admin/usuarios/{usuario_id}/alterar")
def alterar_usuario(request: Request, usuario_id: int, acao: str = Form(...), papel: str = Form(""),
                    filial_id: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    alvo = db.consultar_um("SELECT * FROM usuarios WHERE id = ?", (usuario_id,))
    if alvo is None:
        raise HTTPException(404, "Usuário não encontrado.")
    detalhes = {"usuario_id": usuario_id, "login": alvo["login"], "acao": acao}
    if acao == "resetar_senha":
        senha = secrets.token_urlsafe(9)
        db.executar("UPDATE usuarios SET senha_hash = ?, trocar_senha = 1 WHERE id = ?", (gerar_hash(senha), usuario_id))
        request.session["senha_gerada"] = [alvo["login"], senha]
    elif acao in ("ativar", "desativar"):
        if usuario_id == admin["id"]:
            raise HTTPException(400, "Você não pode desativar o próprio usuário.")
        db.executar("UPDATE usuarios SET ativo = ? WHERE id = ?", (1 if acao == "ativar" else 0, usuario_id))
    elif acao == "papel":
        filial = _filial_para_papel(papel, filial_id)
        detalhes.update(antes={"papel": alvo["papel"], "filial_id": alvo["filial_id"]}, depois={"papel": papel, "filial_id": filial})
        db.executar("UPDATE usuarios SET papel = ?, filial_id = ? WHERE id = ?", (papel, filial, usuario_id))
    else:
        raise HTTPException(400, "Ação inválida.")
    auditoria.registrar("usuario_alterado", request=request, usuario=admin, detalhes=detalhes)
    avisar(request, f"Usuário {alvo['login']} atualizado.")
    return ir("/admin/usuarios")


def _codigo_cadastro(texto: str) -> str | None:
    """Códigos de região/filial: maiúsculas, letras e números, até 12 caracteres (ex.: SP, FN2)."""
    codigo = planilhas.normalizar_texto(texto).replace(" ", "")
    return codigo if codigo.isalnum() and 1 <= len(codigo) <= 12 else None


@app.get("/admin/filiais", response_class=HTMLResponse)
def filiais(request: Request):
    exigir(request, "admin")
    regioes = db.consultar(
        "SELECT r.*, (SELECT COUNT(*) FROM filiais f WHERE f.regiao_id = r.id) AS qtd_filiais, "
        "(SELECT COUNT(*) FROM tabelas_preco t WHERE t.regiao_id = r.id) AS qtd_tabelas FROM regioes r ORDER BY r.codigo")
    lista = db.consultar(
        "SELECT f.*, r.codigo AS regiao_codigo, "
        "(SELECT COUNT(*) FROM lotes l WHERE l.filial_id = f.id) AS qtd_lotes, "
        "(SELECT COUNT(*) FROM usuarios u WHERE u.filial_id = f.id AND u.ativo = 1) AS qtd_usuarios "
        "FROM filiais f JOIN regioes r ON r.id = f.regiao_id ORDER BY f.ativo DESC, f.nome")
    return pagina(request, "filiais.html", regioes=regioes, lista=lista)


@app.post("/admin/regioes/salvar")
def salvar_regiao(request: Request, regiao_id: str = Form(""), codigo: str = Form(...), nome: str = Form(...),
                  csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    cod, nome = _codigo_cadastro(codigo), nome.strip()
    if not cod or not nome:
        avisar(request, "Região precisa de código (letras e números, até 12) e nome.", "erro")
        return ir("/admin/filiais")
    existente = db.consultar_um("SELECT * FROM regioes WHERE codigo = ?", (cod,))
    alvo_id = inteiro_ou_nada(regiao_id)
    if existente and existente["id"] != alvo_id:
        avisar(request, f"Já existe a região {cod}.", "erro")
        return ir("/admin/filiais")
    if alvo_id:
        antes = db.consultar_um("SELECT * FROM regioes WHERE id = ?", (alvo_id,))
        if antes is None:
            raise HTTPException(404, "Região não encontrada.")
        db.executar("UPDATE regioes SET codigo = ?, nome = ? WHERE id = ?", (cod, nome, alvo_id))
        detalhes = {"regiao_id": alvo_id, "antes": dict(antes), "depois": {"codigo": cod, "nome": nome}}
        acao = "regiao_alterada"
    else:
        alvo_id = db.executar("INSERT INTO regioes (codigo, nome) VALUES (?, ?)", (cod, nome))
        detalhes, acao = {"regiao_id": alvo_id, "codigo": cod, "nome": nome}, "regiao_criada"
    auditoria.registrar(acao, request=request, usuario=admin, detalhes=detalhes)
    avisar(request, f"Região {cod} salva.")
    return ir("/admin/filiais")


@app.post("/admin/filiais/salvar")
def salvar_filial(request: Request, filial_id: str = Form(""), codigo: str = Form(""), nome: str = Form(""),
                  regiao_id: str = Form(""), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    cod, nome, regiao = _codigo_cadastro(codigo), nome.strip(), inteiro_ou_nada(regiao_id)
    if not cod or not nome or not regiao or not db.consultar_um("SELECT id FROM regioes WHERE id = ?", (regiao,)):
        avisar(request, "Filial precisa de código (letras e números, até 12), nome e região.", "erro")
        return ir("/admin/filiais")
    existente = db.consultar_um("SELECT * FROM filiais WHERE codigo = ?", (cod,))
    alvo_id = inteiro_ou_nada(filial_id)
    if existente and existente["id"] != alvo_id:
        avisar(request, f"Já existe a filial {cod}.", "erro")
        return ir("/admin/filiais")
    if alvo_id:
        antes = db.consultar_um("SELECT * FROM filiais WHERE id = ?", (alvo_id,))
        if antes is None:
            raise HTTPException(404, "Filial não encontrada.")
        db.executar("UPDATE filiais SET codigo = ?, nome = ?, regiao_id = ? WHERE id = ?", (cod, nome, regiao, alvo_id))
        detalhes = {"filial_id": alvo_id, "antes": dict(antes), "depois": {"codigo": cod, "nome": nome, "regiao_id": regiao}}
        acao = "filial_alterada"
    else:
        alvo_id = db.executar("INSERT INTO filiais (codigo, nome, regiao_id) VALUES (?, ?, ?)", (cod, nome, regiao))
        detalhes, acao = {"filial_id": alvo_id, "codigo": cod, "nome": nome, "regiao_id": regiao}, "filial_criada"
    auditoria.registrar(acao, request=request, usuario=admin, filial_id=alvo_id, detalhes=detalhes)
    avisar(request, f"Filial {cod} salva.")
    return ir("/admin/filiais")


@app.post("/admin/regioes/{regiao_id}/excluir")
def excluir_regiao(request: Request, regiao_id: int, csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    regiao = db.consultar_um("SELECT * FROM regioes WHERE id = ?", (regiao_id,))
    if regiao is None:
        raise HTTPException(404, "Região não encontrada.")
    em_uso = db.consultar_um(
        "SELECT (SELECT COUNT(*) FROM filiais WHERE regiao_id = ?) + (SELECT COUNT(*) FROM tabelas_preco WHERE regiao_id = ?) AS n",
        (regiao_id, regiao_id))["n"]
    if em_uso:
        avisar(request, f"A região {regiao['codigo']} tem filiais ou tabelas e não pode ser excluída. "
                        "Mova as filiais para outra região ou renomeie esta.", "erro")
        return ir("/admin/filiais")
    db.executar("DELETE FROM regioes WHERE id = ?", (regiao_id,))
    auditoria.registrar("regiao_excluida", request=request, usuario=admin, detalhes={"regiao": dict(regiao)})
    avisar(request, f"Região {regiao['codigo']} excluída.")
    return ir("/admin/filiais")


@app.post("/admin/filiais/{filial_id}/excluir")
def excluir_filial(request: Request, filial_id: int, csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    filial = db.consultar_um("SELECT * FROM filiais WHERE id = ?", (filial_id,))
    if filial is None:
        raise HTTPException(404, "Filial não encontrada.")
    em_uso = db.consultar_um(
        "SELECT (SELECT COUNT(*) FROM lotes WHERE filial_id = ?) + (SELECT COUNT(*) FROM usuarios WHERE filial_id = ?) "
        "+ (SELECT COUNT(*) FROM catalogo_fotos WHERE filial_id = ?) AS n", (filial_id, filial_id, filial_id))["n"]
    if em_uso:
        avisar(request, f"A filial {filial['codigo']} tem histórico ou usuários e não pode ser excluída; use desativar.", "erro")
        return ir("/admin/filiais")
    db.executar("DELETE FROM filiais WHERE id = ?", (filial_id,))
    auditoria.registrar("filial_excluida", request=request, usuario=admin, detalhes={"filial": dict(filial)})
    avisar(request, f"Filial {filial['codigo']} excluída.")
    return ir("/admin/filiais")


@app.post("/admin/filiais/{filial_id}/ativo")
def ativar_filial(request: Request, filial_id: int, ativo: str = Form(...), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    filial = db.consultar_um("SELECT * FROM filiais WHERE id = ?", (filial_id,))
    if filial is None:
        raise HTTPException(404, "Filial não encontrada.")
    novo = 1 if ativo == "1" else 0
    db.executar("UPDATE filiais SET ativo = ? WHERE id = ?", (novo, filial_id))
    auditoria.registrar("filial_ativada" if novo else "filial_desativada", request=request, usuario=admin,
                        filial_id=filial_id, detalhes={"codigo": filial["codigo"]})
    avisar(request, f"Filial {filial['codigo']} {'ativada' if novo else 'desativada'}."
            + ("" if novo else " Os usuários dela continuam ativos; troque a filial deles em Usuários se for o caso."))
    return ir("/admin/filiais")


@app.get("/admin/configuracoes", response_class=HTMLResponse)
def configuracoes(request: Request):
    exigir(request, "admin")
    valores = {chave: db.obter_config(chave) for chave in db.CONFIG_PADRAO}
    return pagina(request, "configuracoes.html", valores=valores)


@app.post("/admin/configuracoes")
def salvar_configuracoes(request: Request, variacao_destaque: str = Form(...), regra_preco_minimo: str = Form(...),
                         retencao_anos: str = Form(...), csrf: str = Form("")):
    conferir_csrf(request, csrf)
    admin = exigir(request, "admin")
    novos = {"variacao_destaque": variacao_destaque.replace(",", "."), "regra_preco_minimo": regra_preco_minimo,
             "retencao_anos": retencao_anos}
    try:
        assert 0 <= float(novos["variacao_destaque"]) <= 100
        assert novos["regra_preco_minimo"] in ("fob", "desconto")
        assert 1 <= int(novos["retencao_anos"]) <= 20
    except (AssertionError, ValueError):
        avisar(request, "Valores inválidos.", "erro")
        return ir("/admin/configuracoes")
    mudancas = {}
    with db.transacao() as con:
        for chave, valor in novos.items():
            antigo = db.obter_config(chave)
            if antigo != valor:
                mudancas[chave] = {"antes": antigo, "depois": valor}
                con.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES (?, ?)", (chave, valor))
    auditoria.registrar("configuracao_alterada", request=request, usuario=admin, detalhes=mudancas)
    avisar(request, "Configurações salvas.")
    return ir("/admin/configuracoes")


def _filtrar_eventos(params: dict, limite: int):
    filtros, valores = [], []
    if params.get("usuario"):
        filtros.append("login = ?")
        valores.append(params["usuario"])
    if params.get("tipo"):
        filtros.append("tipo = ?")
        valores.append(params["tipo"])
    if params.get("acao"):
        filtros.append("acao LIKE ?")
        valores.append(f"%{params['acao']}%")
    if params.get("ip"):
        filtros.append("ip = ?")
        valores.append(params["ip"])
    if params.get("de"):
        filtros.append("quando >= ?")
        valores.append(params["de"])
    if params.get("ate"):
        filtros.append("quando <= ?")
        valores.append(params["ate"] + " 23:59:59")
    if params.get("falhas"):
        filtros.append("sucesso = 0")
    if not params.get("com_acessos") and not params.get("tipo"):
        filtros.append("tipo <> 'acesso'")
    onde = ("WHERE " + " AND ".join(filtros)) if filtros else ""
    return db.consultar(f"SELECT * FROM eventos {onde} ORDER BY id DESC LIMIT {int(limite)}", valores)


@app.get("/admin/logs", response_class=HTMLResponse)
def logs(request: Request):
    exigir(request, "admin")
    params = dict(request.query_params)
    eventos = _filtrar_eventos(params, 500)
    logins = [l["login"] for l in db.consultar("SELECT DISTINCT login FROM eventos WHERE login IS NOT NULL ORDER BY login")]
    return pagina(request, "logs.html", eventos=eventos, params=params, logins=logins,
                  consulta=str(request.url.query))


@app.get("/admin/logs.xlsx")
def exportar_logs(request: Request):
    admin = exigir(request, "admin")
    params = dict(request.query_params)
    eventos = _filtrar_eventos(params, 200_000)
    pasta = openpyxl.Workbook()
    aba = pasta.active
    aba.title = "Log"
    colunas = ["id", "quando", "login", "ip", "tipo", "acao", "metodo", "rota", "status", "duracao_ms", "sucesso",
               "filial_id", "lote_id", "detalhes", "navegador"]
    aba.append(colunas)
    for e in eventos:
        aba.append([e[c] for c in colunas])
    arquivo = io.BytesIO()
    pasta.save(arquivo)
    auditoria.registrar("log_exportado", request=request, usuario=admin, detalhes={"filtros": params, "linhas": len(eventos)})
    return Response(arquivo.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="log_{date.today().isoformat()}.xlsx"'})


@app.get("/admin/catalogo", response_class=HTMLResponse)
def catalogo(request: Request):
    exigir(request, "admin")
    fotos = db.consultar(
        "SELECT c.*, f.nome AS filial_nome, u.nome AS usuario_nome FROM catalogo_fotos c "
        "JOIN filiais f ON f.id = c.filial_id LEFT JOIN lotes l ON l.id = c.lote_id "
        "LEFT JOIN usuarios u ON u.id = l.usuario_id ORDER BY c.criado_em DESC LIMIT 300")
    return pagina(request, "catalogo.html", fotos=fotos)
