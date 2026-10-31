"""
Cora MCP — conector do Claude para a conta PJ Cora (Integração Direta).
Ferramentas: saldo, extrato, pagamentos_listar, pagamento_iniciar, pagamento_cancelar.
Todo pagamento iniciado aqui ainda precisa ser APROVADO no app da Cora.
"""
import os, uuid, tempfile, time, datetime as dt
import httpx
from mcp.server.fastmcp import FastMCP

ENV = os.environ.get("CORA_ENV", "stage")            # stage | prod
BASE = {"stage": "https://matls-clients.api.stage.cora.com.br",
        "prod":  "https://matls-clients.api.cora.com.br"}[ENV]
CLIENT_ID = os.environ["CORA_CLIENT_ID"]

def _material(var, fname):
    """Aceita caminho de arquivo OU o conteúdo PEM direto na variável."""
    v = os.environ[var]
    if os.path.exists(v):
        return v
    p = os.path.join(tempfile.gettempdir(), fname)
    with open(p, "w") as f:
        f.write(v.replace("\\n", "\n"))
    os.chmod(p, 0o600)
    return p

CERT = (_material("CORA_CERT", "cora-cert.pem"), _material("CORA_KEY", "cora-key.key"))
SECRET_PATH = os.environ.get("MCP_SECRET_PATH", "mcp")

_tok = {"value": None, "exp": 0}
def token():
    if _tok["value"] and time.time() < _tok["exp"] - 60:
        return _tok["value"]
    with httpx.Client(cert=CERT, timeout=30) as c:
        r = c.post(f"{BASE}/token", data={"grant_type": "client_credentials", "client_id": CLIENT_ID})
        r.raise_for_status()
        d = r.json()
    _tok["value"] = d["access_token"]; _tok["exp"] = time.time() + int(d.get("expires_in", 3600))
    return _tok["value"]

def call(method, path, **kw):
    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    if method == "POST":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    with httpx.Client(cert=CERT, timeout=30) as c:
        r = c.request(method, f"{BASE}{path}", headers=headers, **kw)
    if r.status_code >= 400:
        return {"erro": r.status_code, "detalhe": r.text[:500]}
    return r.json() if r.content else {"ok": True}

reais = lambda cent: round((cent or 0) / 100, 2)

# host 0.0.0.0 no construtor: desliga a proteção DNS-rebinding (só aceitaria Host=localhost,
# e o Railway chega com o domínio público). O segredo do path é a proteção aqui.
mcp = FastMCP("cora", host="0.0.0.0", streamable_http_path=f"/{SECRET_PATH}")

@mcp.tool()
def saldo() -> dict:
    """Saldo atual da conta Cora (em reais)."""
    # Endpoint próprio de saldo; o "end.balance" do extrato vem 0 e não serve para isso.
    b = call("GET", "/third-party/account/balance")
    if "erro" in b: return b
    h = call("GET", "/bank-statement/statement",
             params={"start": dt.date.today().isoformat(), "end": dt.date.today().isoformat(), "perPage": 1})
    return {"saldo_reais": reais(b.get("balance")), "bloqueado_reais": reais(b.get("blockedBalance")),
            "titular": h.get("header", {}).get("businessName") if "erro" not in h else None}

@mcp.tool()
def extrato(inicio: str, fim: str, tipo: str = "") -> dict:
    """Extrato entre duas datas (YYYY-MM-DD). tipo: '' (tudo), CREDIT (entradas) ou DEBIT (saídas)."""
    params = {"start": inicio, "end": fim, "perPage": 200}
    if tipo: params["type"] = tipo
    d = call("GET", "/bank-statement/statement", params=params)
    if "erro" in d: return d
    return {
        "saldo_inicial": reais(d.get("start", {}).get("balance")),
        "saldo_final": reais(d.get("end", {}).get("balance")),
        "total_entradas": reais(d.get("aggregations", {}).get("creditTotal")),
        "total_saidas": reais(d.get("aggregations", {}).get("debitTotal")),
        "lancamentos": [{
            "data": e.get("createdAt", "")[:10], "tipo": e.get("type"), "valor": reais(e.get("amount")),
            "categoria": e.get("transaction", {}).get("type"),
            "descricao": e.get("transaction", {}).get("description"),
            "contraparte": e.get("transaction", {}).get("counterParty", {}).get("name"),
        } for e in d.get("entries", [])],
    }

@mcp.tool()
def pagamentos_listar(inicio: str = "", fim: str = "") -> dict:
    """Pagamentos de boleto iniciados via API que ainda aguardam aprovação no app (período pela data de iniciação)."""
    hoje = dt.date.today()
    params = {"start": inicio or (hoje - dt.timedelta(days=60)).isoformat(), "end": fim or hoje.isoformat(), "size": 100}
    d = call("GET", "/payments", params=params)
    if "erro" in d: return d
    return {"total": d.get("total_items"), "pagamentos": [{
        "id": p.get("id"), "status": p.get("status"), "valor": reais(p.get("amount")),
        "beneficiario": p.get("creditor", {}).get("name"), "vencimento_boleto": p.get("payment_terms", {}).get("due_date"),
        "agendado_para": p.get("scheduled_at"), "referencia": p.get("code"), "iniciado_em": p.get("created_at"),
    } for p in d.get("content", [])]}

@mcp.tool()
def pagamento_iniciar(linha_digitavel: str, agendar_para: str = "", referencia: str = "") -> dict:
    """Inicia (ou agenda, YYYY-MM-DD) o pagamento de um boleto pela linha digitável.
    O pagamento SÓ sai depois de aprovado no app da Cora. 'referencia' é um rótulo seu (ex.: 'vivo-2026-11')."""
    body = {"digitable_line": "".join(ch for ch in linha_digitavel if ch.isdigit())}
    if agendar_para: body["scheduled_at"] = agendar_para
    if referencia: body["code"] = referencia[:64]
    d = call("POST", "/payments/initiate", json=body)
    if "erro" in d: return d
    return {"id": d.get("id"), "status": d.get("status"), "valor": reais(d.get("amount")),
            "beneficiario": d.get("creditor", {}).get("name"), "agendado_para": d.get("scheduled_at"),
            "aprovar_ate": d.get("payment_terms", {}).get("due_date"),
            "aviso": "Abra o app da Cora e aprove este pagamento."}

@mcp.tool()
def pagamento_cancelar(id_pagamento: str) -> dict:
    """Cancela uma iniciação de pagamento ainda não aprovada."""
    return call("DELETE", f"/payments/initiate/{id_pagamento}")

if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")   # modo local: Claude Desktop inicia o servidor sozinho
