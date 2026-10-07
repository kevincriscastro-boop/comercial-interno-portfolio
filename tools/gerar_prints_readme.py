"""Gera os prints da documentação (docs/screenshots) com DADOS FICTÍCIOS.

Sobe o app num banco temporário, cria planilhas inventadas (tabela do gerente e exportação
do Mercos), percorre o fluxo completo (admin, gerente, assistente) e fotografa as telas.
Nenhum dado real é usado.

Uso (na raiz do projeto):
    pip install playwright && python -m playwright install chromium
    python tools/gerar_prints_readme.py
"""
import os
import random
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA_PRINTS = RAIZ / "docs" / "screenshots"
PORTA = 8765
URL = f"http://127.0.0.1:{PORTA}"

DADOS = Path(tempfile.mkdtemp(prefix="comercial_prints_"))
os.environ["APP_DADOS"] = str(DADOS)
os.environ["APP_COOKIE_SEGURO"] = "0"
os.environ["APP_SEGREDO"] = "prints-ficticios"
os.environ["APP_EMPRESA"] = ""  # prints sem marca de empresa
sys.path.insert(0, str(RAIZ))

import openpyxl  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import config, db  # noqa: E402
from app.main import app  # noqa: E402

random.seed(7)

# --------------------------------------------------------------- dados fictícios

PRODUTOS = [  # (código, nome curto do gerente, embalagem, CIF setembro, CIF outubro)
    ("10001", "Óleo Motor 5W30 SN", "CX 12X1LT", 312.40, 309.90),
    ("10002", "Óleo Motor 5W40 SN", "CX 12X1LT", 360.10, 366.50),
    ("10003", "Óleo Motor 10W40 SN", "CX 12X1LT", 331.00, 331.00),
    ("10004", "Óleo Motor 0W20 SP", "CX 12X1LT", 344.80, 352.30),
    ("10005", "Óleo Motor 5W30 SN", "TB 200LT", 4480.00, 4512.75),
    ("10006", "Óleo Diesel 15W40 CI-4", "BB 20LT", 356.20, 349.90),
    ("10007", "Óleo Diesel 15W40 CI-4", "TB 200LT", 3490.00, 3512.75),
    ("10008", "Fluido Hidráulico 68 AW", "BB 20LT", 384.40, 396.28),
    ("10009", "Fluido Hidráulico 68 AW", "TB 200LT", 3845.00, 3964.75),
    ("10010", "Fluido Câmbio 80W90 GL5", "BB 20LT", 486.20, 501.28),
    ("10011", "Fluido Câmbio 85W140 GL5", "BB 20LT", 495.90, 511.28),
    ("10012", "Graxa Lítio EP2", "BD 20KG", 623.15, 504.15),
    ("10013", "Graxa Lítio EP2", "TB 170KG", 4920.75, 4312.75),
    ("10014", "Graxa Cálcio Grafitada", "BD 20KG", 373.15, 324.15),
    ("10015", "Graxa Complexo Vermelha", "BD 20KG", 924.15, 924.15),
    ("10016", "Aditivo Radiador Concentrado", "CX 12X1LT", 266.92, 294.70),
    ("10017", "Aditivo Radiador Pronto Uso", "CX 12X1LT", 190.41, 209.18),
    ("10018", "Fluido Freio DOT 4", "CX 20X500ML", 431.56, 441.18),
    ("10019", "Água Desmineralizada", "CX 12X1LT", 80.68, 83.18),
    ("10020", "Óleo Moto 4T 20W50", "CX 24X1LT", 490.70, 505.88),
    ("10021", "Óleo Moto 4T 10W30", "CX 24X1LT", 553.75, 570.88),
    ("10022", "Óleo Motosserra 2T", "CX 12X500ML", 192.84, 198.80),
    ("10023", "Óleo Compressor ISO 150", "CX 24X1LT", 791.40, 815.88),
    ("10024", "Óleo Engrenagem ISO 220", "BB 20LT", 757.84, 781.28),
    ("10025", "Óleo Engrenagem ISO 320", "BB 20LT", 794.70, 819.28),
]
NOVO_NA_TABELA = [("10026", "Óleo Motor 0W16 SP", "CX 12X1LT", None, 374.95),
                  ("10027", "Óleo Motor 5W20 SP", "CX 12X1LT", None, 344.95)]
FORA_DA_TABELA = [("10090", "BONÉ PROMOCIONAL", 0.0), ("10091", "KIT CHAVEIRO BRINDE", 0.0),
                  ("10092", "DISPLAY BALCÃO", 125.00)]
LIXO_SEM_CODIGO = [("REMOVEDOR DE CARBONO CX 12X300ML", 9), ("LIMPA CONTATO SPRAY 300ML", 4),
                   ("ADITIVO RADIADOR 5LT", 3)]

CABECALHO_MERCOS = [
    "Código do produto\n(recomendado)", "Nome do produto\n(obrigatório)", "Preço de Tabela\n(obrigatório)",
    "Preço Mínimo\n(opcional)", "IPI\n(opcional - Não informar o símbolo %)", "NCM\n(opcional)",
    "Comissão\n(opcional - Não informar o símbolo %)", "Informações adicionais\n(opcional)",
    "Unidade\n(opcional – exemplo: Kg para produtos em quilo, Cx para caixas)",
    "Quantidade em estoque\n(opcional - preencha com um número maior ou igual a 0)", "Múltiplo\n(opcional)",
    "Peso bruto (em Kg)\n(até três casas decimais)", "Tipo peso e dimensões\n(opcional)",
    "Largura da embalagem\n(em centímetros)", "Altura da embalagem\n(em centímetros)",
    "Comprimento da embalagem\n(em centímetros)", "Categoria principal\n(opcional - Máximo 50 caracteres)",
    "Subcategoria nível 2\n(opcional - Máximo 50 caracteres)", "Subcategoria nível 3\n(opcional - Máximo 50 caracteres)",
    "Ativo / Inativo\n(opcional - preencha 0 para tornar o produto ativo ou 1 para tornar o produto inativo)",
    "Exibido / Não exibido no e-commerce\n(opcional - preencha 0 para passar a exibir ou 1 para ocultar)",
    "1. TABELA DE PREÇOS FRETE CIF", "2. TABELA DE PREÇOS FRETE FOB",
] + [f"Preço de Tabela #{n}\n(opcional)" for n in range(3, 20)]


def tabela_gerente(caminho: Path, mes: str, indice_cif: int, extras=()):
    pasta = openpyxl.Workbook()
    aba = pasta.active
    aba.title = "Tabela SP"
    aba.append([f"Tabela {mes} - São Paulo (exemplo fictício)"])
    aba.append([])
    aba.append(["Lubrificantes"])
    aba.append(["Código \nSankhya", "Produto", "Aplicação", "Embalagem", "Custo", "Frete",
                "TABELA DE PREÇOS FRETE CIF", "TABELA DE PREÇOS FRETE FOB", "Margem"])
    for item in list(PRODUTOS) + list(extras):
        cif = item[indice_cif]
        if cif is None:
            continue
        aba.append([int(item[0]), item[1], "Linha automotiva", item[2], round(cif * 0.8, 2), 10.79,
                    cif, round(cif * 0.97, 4), 0.25])
    pasta.save(caminho)


def exportacao_mercos(caminho: Path):
    pasta = openpyxl.Workbook()
    aba = pasta.active
    aba.title = "Planilha1"
    aba.append(CABECALHO_MERCOS)

    def linha(codigo, nome, cif, estoque, ativo=0):
        valores = [None] * len(CABECALHO_MERCOS)
        valores[0], valores[1] = codigo, nome
        valores[2], valores[3] = cif, (round(cif - 10.79, 2) if cif else cif)
        valores[4], valores[5], valores[8], valores[9] = 0, "27101932", "CX", estoque
        valores[11], valores[16] = 11.46, "LUBRIFICANTES"
        valores[19] = valores[20] = ativo
        valores[21], valores[22] = cif, valores[3]
        aba.append(valores)

    for codigo, nome, embalagem, cif_set, _ in PRODUTOS:
        linha(codigo, f"{nome.upper()} {embalagem}", cif_set, random.randint(0, 80))
    linha("10007", "ÓLEO DIESEL 15W40 CI-4 TB 200LT", 0, 2)          # cadastro duplicado
    linha("10015", "FLUIDO DE CORTE SOLÚVEL 20LT", 924.15, 1)         # duplicado com nome trocado
    for codigo, nome, preco in FORA_DA_TABELA:
        linha(codigo, nome, preco, random.randint(0, 30))
    for nome, copias in LIXO_SEM_CODIGO:
        for _ in range(copias):
            linha(None, nome, 0, None)
    pasta.save(caminho)


# --------------------------------------------------------------- app no ar

def subir_app():
    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORTA, log_level="warning"))
    threading.Thread(target=servidor.run, daemon=True).start()
    for _ in range(100):
        if servidor.started:
            return servidor
        time.sleep(0.1)
    raise RuntimeError("o app não subiu")


def senha_inicial() -> str:
    texto = (config.PASTA_DADOS / "SENHA_INICIAL_ADMIN.txt").read_text(encoding="utf-8")
    return re.search(r"Senha temporária: (\S+)", texto).group(1)


def main():
    PASTA_PRINTS.mkdir(parents=True, exist_ok=True)
    servidor = subir_app()
    tabela_set, tabela_out, exportacao = DADOS / "tabela_setembro.xlsx", DADOS / "tabela_outubro.xlsx", DADOS / "export.xlsx"
    tabela_gerente(tabela_set, "Setembro 2026", 3)
    tabela_gerente(tabela_out, "Outubro 2026", 4, NOVO_NA_TABELA)
    exportacao_mercos(exportacao)

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        pagina = navegador.new_page(viewport={"width": 1366, "height": 860}, device_scale_factor=1)

        def foto(nome, inteira=False, recorte=None):
            caminho = PASTA_PRINTS / f"{nome}.png"
            pagina.wait_for_load_state("networkidle")
            time.sleep(0.3)
            if recorte:
                pagina.locator(recorte).first.screenshot(path=str(caminho))
            else:
                pagina.screenshot(path=str(caminho), full_page=inteira)
            print("print:", caminho.relative_to(RAIZ))

        def entrar(login, senha, nova=None):
            pagina.goto(f"{URL}/login")
            pagina.fill("input[name=login]", login)
            pagina.fill("input[name=senha]", senha)
            pagina.click("button:has-text('Entrar')")
            if nova:
                pagina.fill("input[name=atual]", senha)
                pagina.fill("input[name=nova]", nova)
                pagina.fill("input[name=confirmacao]", nova)
                pagina.click("button:has-text('Salvar')")

        def sair():
            pagina.click("button:has-text('Sair')")

        # ---- admin: primeiro acesso e cadastros
        pagina.goto(f"{URL}/login")
        foto("login")
        entrar("admin", senha_inicial(), "Exemplo12345")
        pagina.goto(f"{URL}/admin/filiais")
        for codigo, nome in (("SP", "São Paulo"), ("PR", "Paraná")):
            pagina.fill("#nova-regiao-codigo", codigo)
            pagina.fill("#nova-regiao-nome", nome)
            pagina.click("button:has-text('Criar região')")
        for codigo, nome, regiao in (("DC1", "Distribuidora Centro", "SP"), ("FN2", "Filial Norte", "SP"),
                                     ("FS3", "Filial Sul", "PR")):
            pagina.fill("#nova-filial-codigo", codigo)
            pagina.fill("#nova-filial-nome", nome)
            pagina.select_option("#nova-filial-regiao", label=f"{regiao} — " + ("São Paulo" if regiao == "SP" else "Paraná"))
            pagina.click("button:has-text('Criar filial')")
        foto("filiais", inteira=True)

        senhas = {}
        for login, nome, papel, filial in (("ana.gerente", "Ana (gerente)", "gerente", ""),
                                           ("bruno", "Bruno", "assistente", "Distribuidora Centro"),
                                           ("carla", "Carla", "assistente", "Filial Norte"),
                                           ("diego", "Diego", "assistente", "Filial Sul")):
            pagina.goto(f"{URL}/admin/usuarios")
            formulario = pagina.locator("form[action='/admin/usuarios/criar']")
            formulario.locator("input[name=login]").fill(login)
            formulario.locator("input[name=nome]").fill(nome)
            formulario.locator("select[name=papel]").select_option(papel)
            if filial:
                formulario.locator("select[name=filial_id]").select_option(label=filial)
            formulario.locator("button").click()
            senhas[login] = pagina.locator("p.senha").inner_text()
        foto("usuarios", inteira=True)
        sair()

        # ---- gerente: tabela de setembro publicada, outubro em rascunho
        entrar("ana.gerente", senhas["ana.gerente"], "Gerente12345")
        for arquivo, vigencia in ((tabela_set, "2026-09-01"), (tabela_out, "2026-10-01")):
            pagina.goto(f"{URL}/tabelas")
            pagina.select_option("select[name=regiao_id]", label="SP — São Paulo")
            pagina.fill("input[name=vigencia]", vigencia)
            pagina.fill("input[name=desconto_max]", "10")
            pagina.fill("input[name=observacao]", "Reajuste mensal (exemplo)")
            pagina.set_input_files("input[name=arquivo]", str(arquivo))
            pagina.click("button:has-text('Conferir tabela')")
            if arquivo == tabela_out:
                foto("tabela-conferencia", inteira=True)
            pagina.check("input[name=confirmo_filiais]")
            pagina.click("button:has-text('Publicar tabela')")
        pagina.goto(f"{URL}/tabelas")
        foto("tabelas")
        sair()

        # ---- assistente: importação da filial
        entrar("bruno", senhas["bruno"], "Assistente1234")
        pagina.goto(f"{URL}/importacao")
        foto("importacao")
        pagina.set_input_files("input[name=arquivo]", str(exportacao))
        pagina.click("button:has-text('Enviar e conferir')")
        foto("importacao-conferencia", inteira=True)
        pagina.click("button:has-text('Gerar arquivo de importação')")
        foto("importacao-gerada")
        sair()

        # ---- admin: visão geral, log e catálogos
        entrar("admin", "Exemplo12345")
        pagina.goto(f"{URL}/")
        foto("inicio")
        pagina.goto(f"{URL}/admin/logs")
        foto("log")
        pagina.goto(f"{URL}/admin/catalogo")
        foto("catalogos")
        navegador.close()
    servidor.should_exit = True


if __name__ == "__main__":
    db.inicializar()
    main()
