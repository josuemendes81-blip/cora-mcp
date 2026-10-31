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

# ─────────────────────────── Pluggy (Open Finance) ───────────────────────────
# Variáveis: PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET. Opcional: PLUGGY_ITEM_IDS (ids separados
# por vírgula) caso a API não liste os items da aplicação.
PLUGGY_BASE = "https://api.pluggy.ai"
_pk = {"value": None, "exp": 0}

def _pluggy_key():
    if _pk["value"] and time.time() < _pk["exp"]:
        return _pk["value"]
    cid, sec = os.environ.get("PLUGGY_CLIENT_ID"), os.environ.get("PLUGGY_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("PLUGGY_CLIENT_ID/PLUGGY_CLIENT_SECRET não configurados")
    r = httpx.post(f"{PLUGGY_BASE}/auth", json={"clientId": cid, "clientSecret": sec}, timeout=30)
    r.raise_for_status()
    _pk["value"] = r.json()["apiKey"]
    _pk["exp"] = time.time() + 2 * 3600 - 300          # apiKey vale 2h; renova 5 min antes
    return _pk["value"]

def pcall(method, path, **kw):
    try:
        key = _pluggy_key()
    except Exception as e:
        return {"erro": "auth", "detalhe": str(e)[:300]}
    for tentativa in (1, 2):
        r = httpx.request(method, f"{PLUGGY_BASE}{path}", headers={"X-API-KEY": key}, timeout=30, **kw)
        if r.status_code == 401 and tentativa == 1:     # chave expirada antes da hora: renova uma vez
            _pk["value"] = None; key = _pluggy_key(); continue
        break
    if r.status_code >= 400:
        return {"erro": r.status_code, "detalhe": r.text[:500]}
    return r.json() if r.content else {"ok": True}

_rs = lambda v: round(v, 2) if isinstance(v, (int, float)) else v      # Pluggy já devolve reais
_dia = lambda s: (s or "")[:10] or None

def _conta(a):
    cd = a.get("creditData") or {}
    return {"id": a.get("id"), "nome": a.get("name"), "tipo": a.get("type"), "subtipo": a.get("subtype"),
            "numero": a.get("number"), "saldo_reais": _rs(a.get("balance")), "moeda": a.get("currencyCode"),
            **({"limite_reais": _rs(cd.get("creditLimit")), "limite_disponivel_reais": _rs(cd.get("availableCreditLimit")),
                "vencimento_fatura": _dia(cd.get("balanceDueDate")), "fechamento_fatura": _dia(cd.get("balanceCloseDate"))}
               if cd else {})}

@mcp.tool()
def pluggy_contas() -> dict:
    """Instituições conectadas na Pluggy (items) e as contas/cartões de cada uma, com saldo em reais."""
    d = pcall("GET", "/items")
    if "erro" in d:
        ids = [i.strip() for i in os.environ.get("PLUGGY_ITEM_IDS", "").split(",") if i.strip()]
        if not ids: return d
        items = [pcall("GET", f"/items/{i}") for i in ids]
    else:
        items = d.get("results", d if isinstance(d, list) else [])
    out = []
    for it in items:
        if "erro" in it: out.append(it); continue
        acc = pcall("GET", "/accounts", params={"itemId": it.get("id")})
        out.append({"item_id": it.get("id"), "instituicao": (it.get("connector") or {}).get("name"),
                    "status": it.get("status"), "atualizado_em": _dia(it.get("lastUpdatedAt")),
                    "contas": acc if "erro" in acc else [_conta(a) for a in acc.get("results", [])]})
    return {"items": out}

@mcp.tool()
def pluggy_extrato(account_id: str, inicio: str, fim: str) -> dict:
    """Transações de uma conta Pluggy entre duas datas (YYYY-MM-DD), todas as páginas. Valores em reais (negativo = saída)."""
    # /transactions (v1) foi descontinuado (410); v2 usa dateFrom/dateTo e cursor em "next".
    from urllib.parse import parse_qsl
    base = {"accountId": account_id, "dateFrom": inicio, "dateTo": fim}
    tx, params = [], dict(base)
    for _ in range(100):
        d = pcall("GET", "/v2/transactions", params=params)
        if "erro" in d: return d
        tx += d.get("results", [])
        nxt = d.get("next")
        if not nxt: break
        params = {**base, **dict(parse_qsl(nxt.split("?", 1)[-1]))}
    lanc = [{"data": _dia(t.get("date")), "descricao": t.get("description"), "valor_reais": _rs(t.get("amount")),
             "tipo": t.get("type"), "categoria": t.get("category"), "status": t.get("status"),
             **({"parcela": f'{t["creditCardMetadata"].get("installmentNumber")}/{t["creditCardMetadata"].get("totalInstallments")}'}
                if (t.get("creditCardMetadata") or {}).get("totalInstallments") else {})} for t in tx]
    ent = sum(t["valor_reais"] for t in lanc if t["tipo"] == "CREDIT")
    sai = sum(t["valor_reais"] for t in lanc if t["tipo"] == "DEBIT")
    return {"total": len(lanc), "total_entradas_reais": round(ent, 2), "total_saidas_reais": round(sai, 2),
            "lancamentos": lanc}

@mcp.tool()
def pluggy_faturas(account_id: str) -> dict:
    """Faturas de um cartão de crédito conectado na Pluggy (valores em reais, datas YYYY-MM-DD)."""
    d = pcall("GET", "/bills", params={"accountId": account_id})
    if "erro" in d: return d
    return {"faturas": [{"id": b.get("id"), "vencimento": _dia(b.get("dueDate")),
                         "total_reais": _rs(b.get("totalAmount")), "minimo_reais": _rs(b.get("minimumPaymentAmount")),
                         "moeda": b.get("totalAmountCurrencyCode")} for b in d.get("results", [])]}

@mcp.tool()
def pluggy_atualizar(item_id: str) -> dict:
    """Pede à Pluggy para sincronizar de novo uma instituição (item). Os dados chegam em alguns minutos."""
    d = pcall("PATCH", f"/items/{item_id}", json={})
    if "erro" in d: return d
    return {"item_id": d.get("id"), "status": d.get("status"), "execucao": d.get("executionStatus"),
            "aviso": "Atualização solicitada; consulte pluggy_contas em alguns minutos."}

if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")   # modo local: Claude Desktop inicia o servidor sozinho
