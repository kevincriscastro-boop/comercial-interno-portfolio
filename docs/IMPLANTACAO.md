# Implantação na VPS (aaPanel)

Registro de como o app foi implantado em 07/10/2026, do zero, num servidor com aaPanel. A VPS é **compartilhada** com outros
sites: nada aqui altera nginx, PHP, MySQL ou outros sites.

## Visão geral

```
navegador ──HTTPS──► nginx (aaPanel, portas 80/443) ──► app em 127.0.0.1:8100 (usuário www)
                       │                                   │
                       └ só IPs liberados (403 p/ o resto)  └ dados em /www/comercialinterno_dados
```

| Item | Onde |
|---|---|
| Código (clone do GitHub) | `/www/wwwroot/comercialinterno` |
| Configuração de produção | `/www/wwwroot/comercialinterno/.env` (dono `www`, `chmod 600`, fora do Git) |
| Dados: banco, planilhas, backups | `/www/comercialinterno_dados` (dono `www`, `chmod 700`) |
| Ambiente Python (venv) | `/www/server/pyporject_evn/comercialinterno_sys` (Python 3.12 do sistema) |
| Log do app (saída do processo) | `/www/wwwlogs/python/comercialinterno/error.log` |
| Log de acesso do nginx | `/www/wwwlogs/comercialinterno.log` |
| Restrição de IPs | `/www/server/panel/vhost/nginx/extension/comercialinterno/restricao_ip.conf` |
| Certificado | Let's Encrypt, renovação automática pelo aaPanel 1 mês antes de vencer |

## Como foi feito (passo a passo)

1. **Chave de implantação (somente leitura) para o repositório privado**
   ```bash
   ssh-keygen -t ed25519 -C "vps-comercialinterno" -f /root/.ssh/comercialinterno_deploy -N ""
   cat /root/.ssh/comercialinterno_deploy.pub   # cadastrada no GitHub como deploy key read-only
   cat >> /root/.ssh/config <<'EOF'
   Host github-comercialinterno
     HostName github.com
     User git
     IdentityFile /root/.ssh/comercialinterno_deploy
     IdentitiesOnly yes
   EOF
   chmod 600 /root/.ssh/config
   ssh -T github-comercialinterno   # fingerprint oficial do GitHub: SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
   ```
2. **Código e pasta de dados**
   ```bash
   cd /www/wwwroot && git clone github-comercialinterno:SEU_USUARIO/comercial-interno.git comercialinterno
   mkdir -p /www/comercialinterno_dados && chmod 700 /www/comercialinterno_dados
   # o aaPanel passa a pasta do código para o usuário www; o git (rodando como root) pede esta confirmação uma vez
   git config --global --add safe.directory /www/wwwroot/comercialinterno
   ```
3. **`.env` de produção** (chave gerada na própria VPS)
   ```bash
   cd /www/wwwroot/comercialinterno
   python3 - <<'EOF'
   import secrets
   open('.env', 'w').write(f"APP_SEGREDO={secrets.token_hex(32)}\nAPP_DADOS=/www/comercialinterno_dados\n"
                           "APP_COOKIE_SEGURO=1\nAPP_CONFIAR_PROXY=1\n")
   EOF
   chown www:www .env && chmod 600 .env
   chown -R www:www /www/comercialinterno_dados
   ```
4. **Ambiente virtual**: o Ubuntu precisou do pacote `python3.12-venv` (`apt-get install -y python3.12-venv`).
   No aaPanel: *Website → Python Project → Python Environment → Create virtual environment*,
   nome `comercialinterno_sys`, origem **System env** (3.12.3).
   > Não use o "pyenv (aaPanel env)": é o Python do próprio painel, acessível só pelo root, e o app roda como `www`.
5. **Projeto Python** (*Add Project*): ambiente `comercialinterno_sys`, *Command Startup*,
   caminho `/www/wwwroot/comercialinterno`, comando
   `python -m uvicorn app.main:app --host 127.0.0.1 --port 8100`, usuário `www`,
   dependências `/www/wwwroot/comercialinterno/requirements.txt`.
6. **Domínio**: *Domain Manager* → `comercialinterno.suaempresa.com.br`; *Mapping* → ativar
   "External network mapping". A porta 8100 continua **fechada** no firewall.
7. **HTTPS**: *SSL → Let's Encrypt* → emitir; *Force HTTPS* ligado.
8. **Restrição de IPs** (arquivo de extensão, que o aaPanel não sobrescreve):
   ```nginx
   set $ci_bloqueado 1;
   if ($remote_addr = 203.0.113.10) { set $ci_bloqueado 0; }
   if ($remote_addr = 198.51.100.20)   { set $ci_bloqueado 0; }
   if ($remote_addr = 127.0.0.1)       { set $ci_bloqueado 0; }
   if ($uri ~ "^/\.well-known/acme-challenge/") { set $ci_bloqueado 0; }
   if ($ci_bloqueado) { return 403; }
   ```
   Aplicar sempre com teste antes: `/www/server/nginx/sbin/nginx -t && /www/server/nginx/sbin/nginx -s reload`

## Depois da implantação

Atualizar o app, voltar versão, agendar o backup diário (Cron), restaurar backup, liberar IP novo,
recuperar a senha do admin e problemas comuns: veja [OPERACAO.md](OPERACAO.md).

Primeiro acesso: a senha temporária do `admin` fica em `/www/comercialinterno_dados/SENHA_INICIAL_ADMIN.txt`.
Entre por `https://`, troque a senha e apague o arquivo
(`rm /www/comercialinterno_dados/SENHA_INICIAL_ADMIN.txt`). Depois cadastre regiões, filiais e usuários
(ver [USO.md](USO.md#administrador)).
