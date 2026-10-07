"""Manutenção diária: backup do banco e limpeza do que passou do prazo de guarda.

Uso (Cron do aaPanel, todo dia de madrugada):
    python -m app.manutencao
"""
import sqlite3
from datetime import date, datetime, timedelta

from . import auditoria, config, db

BACKUPS_GUARDADOS = 30


def fazer_backup() -> str:
    destino = config.PASTA_BACKUP / f"precos_{datetime.now():%Y-%m-%d_%H%M}.db"
    origem = sqlite3.connect(config.BANCO)
    copia = sqlite3.connect(destino)
    with copia:
        origem.backup(copia)  # cópia consistente mesmo com o app rodando
    copia.close()
    origem.close()
    antigos = sorted(config.PASTA_BACKUP.glob("precos_*.db"))[:-BACKUPS_GUARDADOS]
    for arquivo in antigos:
        arquivo.unlink()
    return str(destino)


def limpar_vencidos() -> dict:
    anos = int(db.obter_config("retencao_anos"))
    limite = (date.today() - timedelta(days=365 * anos)).isoformat()
    apagados = 0
    for arquivo in db.consultar("SELECT * FROM arquivos WHERE criado_em < ?", (limite,)):
        caminho = auditoria.caminho_arquivo(arquivo)
        if caminho.exists():
            caminho.unlink()
            apagados += 1
    with db.transacao() as con:
        eventos = con.execute("DELETE FROM eventos WHERE quando < ?", (limite,)).rowcount
    return {"limite": limite, "arquivos_apagados": apagados, "eventos_apagados": eventos}


def principal() -> None:
    db.inicializar()
    try:
        destino = fazer_backup()
        limpeza = limpar_vencidos()
        livre = __import__("shutil").disk_usage(config.PASTA_DADOS).free // (1024 * 1024)
        auditoria.registrar("manutencao", tipo="sistema",
                            detalhes={"backup": destino, **limpeza, "disco_livre_mb": livre})
        print(f"Backup: {destino} | {limpeza} | disco livre: {livre} MB")
    except Exception as erro:
        auditoria.registrar("manutencao", tipo="sistema", sucesso=False, detalhes={"erro": repr(erro)})
        raise


if __name__ == "__main__":
    principal()
