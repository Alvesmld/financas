import os
import json
import io
import csv
import sqlite3
import hashlib
import secrets
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image

from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SENHA_FAMILIAR_MESTRA = os.getenv("SENHA_FAMILIAR_MESTRA", "").strip()


import requests


def _ai_provider_order():
    """Lista de provedores configurável; os indisponíveis são ignorados na tentativa."""
    raw = os.getenv("AI_PROVIDERS", "gemini,openai,ollama")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _extract_response_text(value):
    text = (value or "").strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _generate_text_with_provider(system_prompt: str, user_prompt: str) -> tuple[str, str]:
    """Tenta provedores de IA em sequência. Ollama funciona quando o backend alcança o host local."""
    failures = []
    for provider in _ai_provider_order():
        try:
            if provider == "gemini":
                key = os.getenv("GEMINI_API_KEY", "").strip()
                if not key:
                    failures.append("gemini: GEMINI_API_KEY ausente")
                    continue
                from google import genai
                client = genai.Client(api_key=key)
                models = [os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")]
                for model in dict.fromkeys(models):
                    try:
                        response = client.models.generate_content(model=model, contents=[system_prompt, user_prompt])
                        answer = getattr(response, "text", None)
                        if answer and answer.strip():
                            return answer.strip(), f"gemini:{model}"
                        failures.append(f"gemini:{model}: resposta vazia")
                    except Exception as exc:
                        failures.append(f"gemini:{model}: {type(exc).__name__}: {exc}")
            elif provider in ("openai", "openai_compatible", "openrouter", "lmstudio"):
                key = os.getenv("OPENAI_API_KEY", "").strip()
                base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
                model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
                if not key and base.startswith("https://api.openai.com"):
                    failures.append("openai: OPENAI_API_KEY ausente")
                    continue
                response = requests.post(base + "/chat/completions", timeout=90,
                    headers={"Authorization": f"Bearer {key}" if key else "", "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.2, "messages": [
                        {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}
                    ]})
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]["content"]
                if isinstance(answer, list):
                    answer = "\n".join(str(x.get("text", "")) for x in answer if isinstance(x, dict))
                if answer and str(answer).strip():
                    return str(answer).strip(), f"{provider}:{model}"
                failures.append(f"{provider}: resposta vazia")
            elif provider == "ollama":
                base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
                model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
                response = requests.post(base + "/api/chat", timeout=120,
                    json={"model": model, "stream": False, "messages": [
                        {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}
                    ], "options": {"temperature": 0.2}})
                response.raise_for_status()
                answer = response.json().get("message", {}).get("content", "")
                if answer.strip():
                    return answer.strip(), f"ollama:{model}"
                failures.append("ollama: resposta vazia")
            else:
                failures.append(f"provedor desconhecido: {provider}")
        except Exception as exc:
            failures.append(f"{provider}: {type(exc).__name__}: {exc}")
    import logging
    logging.error("Todos os provedores de IA falharam: %s", " | ".join(failures))
    raise RuntimeError("Nenhum provedor de IA respondeu. Confira as variáveis e os logs do backend.")

try:
    from google import genai
    from google.genai import types
    HAS_GEMINI_SDK = True if GEMINI_API_KEY else False
except ImportError:
    HAS_GEMINI_SDK = False

app = FastAPI(
    title="Finanças Domésticas AI - ALVESMLD SaaS Master",
    description="Backend completo com todas as funcionalidades, IA, Licenciamento Cakto e Anti-Sharing",
    version="34.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "financas.db"))

@app.get("/", include_in_schema=False)
def servir_frontend():
    index_file = BASE_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {"detail": "Ficheiro index.html não encontrado."}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                sessao_token TEXT
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS licencas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chave TEXT UNIQUE NOT NULL,
                email TEXT,
                status TEXT DEFAULT 'disponivel',
                criado_em TEXT NOT NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                tipo TEXT NOT NULL,
                categoria TEXT NOT NULL,
                data TEXT NOT NULL,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contas_a_pagar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                descricao TEXT NOT NULL,
                valor REAL NOT NULL,
                data_vencimento TEXT NOT NULL,
                categoria TEXT NOT NULL,
                status TEXT DEFAULT 'pendente',
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metas_economia (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                valor_objetivo REAL NOT NULL,
                valor_atual REAL DEFAULT 0.0,
                data_limite TEXT,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lista_desejos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                titulo TEXT NOT NULL,
                valor REAL NOT NULL,
                link TEXT,
                status TEXT DEFAULT 'pendente',
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orcamentos_categoria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                categoria TEXT NOT NULL,
                limite_mensal REAL NOT NULL,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS compras_cakto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                criado_em TEXT NOT NULL,
                ativada INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migração segura para instalações existentes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_compras_email ON compras_cakto(email)")

        cursor.execute("SELECT COUNT(*) FROM usuarios WHERE email = 'demo@financas.com'")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO usuarios (nome, email, senha_hash)
                VALUES ('Usuário Convidado', 'demo@financas.com', ?)
            """, (hashlib.sha256('123456'.encode()).hexdigest(),))
            
        conn.commit()

init_db()

def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

class UsuarioLogin(BaseModel):
    email: str
    senha: str

class AtivarLicencaSchema(BaseModel):
    chave: Optional[str] = ""
    nome: str
    email: str
    senha: str

class TransacaoSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    descricao: str
    valor: float
    tipo: str
    categoria: str
    data: str

class OrcamentoSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    categoria: str
    limite_mensal: float

class DesejoSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    titulo: str
    valor: float
    link: Optional[str] = ""
    status: Optional[str] = "pendente"

class ChatPrompt(BaseModel):
    usuario_id: int
    pergunta: str
    ficheiro_base64: Optional[str] = None
    mime_type: Optional[str] = None

@app.post("/api/login")
def login_usuario(credenciais: UsuarioLogin):
    email_limpo = credenciais.email.lower().strip()
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if SENHA_FAMILIAR_MESTRA and secrets.compare_digest(credenciais.senha, SENHA_FAMILIAR_MESTRA):
            u = cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
            if not u:
                cursor.execute("""
                    INSERT INTO usuarios (nome, email, senha_hash) VALUES (?, ?, ?)
                """, ("Familiar (Pais)", email_limpo, hash_senha(credenciais.senha)))
                conn.commit()
                u = cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
        else:
            u = cursor.execute("SELECT * FROM usuarios WHERE email = ? AND senha_hash = ?", 
                               (email_limpo, hash_senha(credenciais.senha))).fetchone()
        
        if not u:
            raise HTTPException(status_code=401, detail="E-mail ou senha incorretos ou acesso não autorizado.")
        
        novo_token = secrets.token_hex(32)
        cursor.execute("UPDATE usuarios SET sessao_token = ? WHERE id = ?", (novo_token, u["id"]))
        conn.commit()
        
        return {
            "sucesso": True, 
            "usuario": {"id": u["id"], "nome": u["nome"], "email": u["email"]},
            "sessao_token": novo_token
        }

@app.post("/api/verificar-sessao")
def verificar_sessao(usuario_id: int = Query(...), token: str = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        u = cursor.execute("SELECT sessao_token FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if not u or u["sessao_token"] != token:
            raise HTTPException(status_code=401, detail="Sessão encerrada por outro acesso simultâneo.")
        return {"valido": True}

@app.post("/api/ativar-licenca")
def ativar_licenca(payload: AtivarLicencaSchema):
    email_limpo = payload.email.lower().strip()
    if len(payload.senha) < 8:
        raise HTTPException(status_code=400, detail="Crie uma senha com pelo menos 8 caracteres.")
    if not email_limpo or "@" not in email_limpo:
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        existente = cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
        if existente:
            raise HTTPException(status_code=400, detail="Este e-mail já possui uma conta. Entre com sua senha.")

        chave = (payload.chave or "").strip()
        lic = None
        compra = None
        if chave:
            lic = cursor.execute(
                "SELECT * FROM licencas WHERE chave = ? AND status = 'disponivel'",
                (chave,)
            ).fetchone()
            if not lic:
                raise HTTPException(status_code=400, detail="Chave inválida ou já utilizada.")
            if lic["email"] and lic["email"].lower() != email_limpo:
                raise HTTPException(status_code=403, detail="Esta licença pertence a outro e-mail.")
        else:
            compra = cursor.execute("""
                SELECT id FROM compras_cakto
                WHERE lower(email) = ? AND status = 'approved' AND ativada = 0
                ORDER BY id DESC LIMIT 1
            """, (email_limpo,)).fetchone()
            if not compra:
                raise HTTPException(status_code=403, detail="Não encontramos uma compra aprovada para este e-mail. Use o mesmo e-mail da compra na Cakto ou informe sua chave de licença.")

        cursor.execute(
            "INSERT INTO usuarios (nome, email, senha_hash) VALUES (?, ?, ?)",
            (payload.nome.strip(), email_limpo, hash_senha(payload.senha))
        )
        uid = cursor.lastrowid
        if lic:
            cursor.execute("UPDATE licencas SET status = 'utilizada', email = ? WHERE id = ?", (email_limpo, lic["id"]))
        if compra:
            cursor.execute("UPDATE compras_cakto SET ativada = 1 WHERE id = ?", (compra["id"],))
        conn.commit()
        return {"sucesso": True, "usuario": {"id": uid, "nome": payload.nome.strip(), "email": email_limpo}}

@app.post("/api/webhook/cakto")
async def webhook_cakto(request: Request):
    """
    Registra compras aprovadas por e-mail para liberar o cadastro automaticamente.
    Configure CAKTO_WEBHOOK_SECRET no Render e envie o mesmo valor no header
    X-Webhook-Secret. O formato do payload pode variar; extraímos campos comuns.
    """
    secret_configurado = os.getenv("CAKTO_WEBHOOK_SECRET", "").strip()
    if secret_configurado:
        recebido = request.headers.get("x-webhook-secret", "") or request.query_params.get("secret", "")
        if not secrets.compare_digest(recebido, secret_configurado):
            raise HTTPException(status_code=401, detail="Webhook não autorizado.")

    try:
        dados = await request.json()
        # Alguns eventos colocam os dados do pedido dentro de data/order/customer.
        def nested(obj, *keys):
            atual = obj
            for key in keys:
                if not isinstance(atual, dict):
                    return None
                atual = atual.get(key)
            return atual

        status_raw = (
            dados.get("status") or dados.get("payment_status") or dados.get("event")
            or nested(dados, "data", "status") or nested(dados, "data", "event")
            or nested(dados, "order", "status")
        )
        status = str(status_raw or "").lower().strip()
        email = (
            dados.get("email") or dados.get("customer_email")
            or nested(dados, "customer", "email")
            or nested(dados, "buyer", "email")
            or nested(dados, "data", "customer", "email")
            or nested(dados, "data", "buyer", "email")
            or nested(dados, "order", "customer", "email")
        )
        order_id = (
            dados.get("id") or dados.get("order_id") or dados.get("transaction_id")
            or nested(dados, "data", "id") or nested(dados, "order", "id")
        )
        approved = status in {"approved", "paid", "order.paid", "payment.approved", "purchase_approved", "completed"}
        if not approved:
            return {"status": "ignorado", "motivo": "evento não representa pagamento aprovado"}
        if not email or "@" not in str(email):
            raise HTTPException(status_code=400, detail="Pagamento aprovado, mas o e-mail do comprador não foi encontrado no payload da Cakto.")

        email = str(email).strip().lower()
        order_id = str(order_id).strip() if order_id is not None else None
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            if order_id:
                cursor.execute("""
                    INSERT OR IGNORE INTO compras_cakto (order_id, email, status, criado_em)
                    VALUES (?, ?, 'approved', ?)
                """, (order_id, email, str(date.today())))
                # Se já existe o pedido, atualiza o e-mail/status sem duplicar compra.
                cursor.execute("UPDATE compras_cakto SET email = ?, status = 'approved' WHERE order_id = ?", (email, order_id))
            else:
                # Sem ID do pedido não há idempotência perfeita; gera uma chave de deduplicação pelo payload.
                fingerprint = hashlib.sha256(json.dumps(dados, sort_keys=True, default=str).encode()).hexdigest()
                cursor.execute("""
                    INSERT OR IGNORE INTO compras_cakto (order_id, email, status, criado_em)
                    VALUES (?, ?, 'approved', ?)
                """, (fingerprint, email, str(date.today())))
            conn.commit()
        return {"status": "compra_registrada", "email": email, "proximo_passo": "O comprador pode criar a conta usando este mesmo e-mail."}
    except HTTPException:
        raise
    except Exception:
        # Não expor detalhes internos do servidor no endpoint público.
        raise HTTPException(status_code=400, detail="Payload de webhook inválido.")

@app.post("/api/scanner-contas-foto")
async def scanner_contas_foto(
    usuario_id: int = Form(...),
    foto: UploadFile = File(...)
):
    if not HAS_GEMINI_SDK:
        raise HTTPException(status_code=400, detail="O scanner de imagens/PDF precisa de GEMINI_API_KEY configurada. Chat de texto aceita Gemini, OpenAI compatível e Ollama.")

    try:
        conteudo_ficheiro = await foto.read()
        mime = foto.content_type or "image/jpeg"
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        if "pdf" in mime:
            part_file = types.Part.from_bytes(data=conteudo_ficheiro, mime_type="application/pdf")
        else:
            part_file = Image.open(io.BytesIO(conteudo_ficheiro))

        hoje_br = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        prompt_vision = f"""
        Analise este documento ou imagem. Data atual: {hoje_br.isoformat()}.
        Extraia o mês e ano que aparecem no documento (competência/fatura) em mes_ano_detectado, por exemplo Outubro/2026. Não invente 2023 se o documento não mostrar esse ano. Em data_vencimento, use a data de vencimento impressa; se não houver, use {hoje_br.isoformat()} e sinalize isso na descrição.
        Retorne APENAS um JSON estrito:
        {
          "mes_ano_detectado": "Outubro/2026",
          "contas": [{"descricao": "...", "valor": 0.0, "categoria": "Outros", "status": "pendente", "data_vencimento": "YYYY-MM-DD"}]
        }
        """
        response = None
        falhas = []
        for modelo in dict.fromkeys([os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")]):
            try:
                response = client.models.generate_content(model=modelo, contents=[part_file, prompt_vision])
                if response and getattr(response, "text", None):
                    break
                response = None
            except Exception as exc:
                falhas.append(f"{modelo}: {type(exc).__name__}: {exc}")
        if response is None or not getattr(response, "text", None):
            raise RuntimeError("Nenhum modelo Gemini respondeu: " + " | ".join(falhas))
        texto_limpo = response.text.strip().replace("```json", "").replace("```JSON", "").replace("```", "").strip()
        ini, fim = texto_limpo.find("{"), texto_limpo.rfind("}")
        if ini < 0 or fim <= ini:
            raise ValueError("A IA não retornou JSON válido para o documento")
        dados_ia = json.loads(texto_limpo[ini:fim+1])
        
        total = 0
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            for c in dados_ia.get("contas", []):
                cursor.execute("""
                    INSERT INTO contas_a_pagar (usuario_id, descricao, valor, data_vencimento, categoria, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (usuario_id, c.get("descricao"), float(c.get("valor", 0.0)), c.get("data_vencimento"), c.get("categoria", "Outros"), c.get("status", "pendente")))
                total += 1
            conn.commit()

        return {"sucesso": True, "total_importadas": total, "mes_ano_detectado": dados_ia.get("mes_ano_detectado")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/transacoes")
def listar_transacoes(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ? ORDER BY data DESC, id DESC", (usuario_id,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/transacoes")
def criar_transacao(transacao: TransacaoSchema):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (transacao.usuario_id, transacao.descricao, transacao.valor, transacao.tipo, transacao.categoria, transacao.data))
        conn.commit()
        transacao.id = cursor.lastrowid
        return transacao

@app.delete("/api/transacoes/{transacao_id}")
def deletar_transacao(transacao_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transacoes WHERE id = ? AND usuario_id = ?", (transacao_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.get("/api/projecao/{meses}")
def api_projecao(meses: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (usuario_id,)).fetchall()
        contas = cursor.execute("SELECT * FROM contas_a_pagar WHERE usuario_id = ? AND status != 'paga'", (usuario_id,)).fetchall()
        
        entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
        saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
        saldo_atual = entradas - saidas
        data_limite = date.today() + timedelta(days=30 * meses)
        total_contas = sum(c["valor"] for c in contas if c["data_vencimento"] <= str(data_limite))
        saldo_final = saldo_atual + (entradas * meses) - (saidas * meses) - total_contas
        
        return {"periodo_meses": meses, "saldo_atual": round(saldo_atual, 2), "entradas_estimadas": round(entradas * meses, 2), "saidas_estimadas": round(saidas * meses, 2), "contas_agendadas": round(total_contas, 2), "saldo_projetado_final": round(saldo_final, 2)}

@app.get("/api/orcamentos")
def listar_orcamentos(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        orcamentos = cursor.execute("SELECT * FROM orcamentos_categoria WHERE usuario_id = ?", (usuario_id,)).fetchall()
        transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ? AND tipo = 'saida'", (usuario_id,)).fetchall()
        
        resultado = []
        for o in orcamentos:
            gasto_atual = sum(t["valor"] for t in transacoes if t["categoria"] == o["categoria"])
            resultado.append({
                "id": o["id"], "categoria": o["categoria"], "limite_mensal": o["limite_mensal"],
                "gasto_atual": round(gasto_atual, 2), "estourado": gasto_atual > o["limite_mensal"]
            })
        return resultado

@app.post("/api/orcamentos")
def definir_orcamento(orcamento: OrcamentoSchema):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        existente = cursor.execute("SELECT id FROM orcamentos_categoria WHERE usuario_id = ? AND categoria = ?", (orcamento.usuario_id, orcamento.categoria)).fetchone()
        if existente:
            cursor.execute("UPDATE orcamentos_categoria SET limite_mensal = ? WHERE id = ?", (orcamento.limite_mensal, existente[0]))
        else:
            cursor.execute("INSERT INTO orcamentos_categoria (usuario_id, categoria, limite_mensal) VALUES (?, ?, ?)", (orcamento.usuario_id, orcamento.categoria, orcamento.limite_mensal))
        conn.commit()
        return {"sucesso": True}

@app.delete("/api/orcamentos/{orc_id}")
def deletar_orcamento(orc_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM orcamentos_categoria WHERE id = ? AND usuario_id = ?", (orc_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.get("/api/desejos", response_model=List[DesejoSchema])
def listar_desejos(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM lista_desejos WHERE usuario_id = ?", (usuario_id,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/desejos", response_model=DesejoSchema)
def criar_desejo(desejo: DesejoSchema):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO lista_desejos (usuario_id, titulo, valor, link, status)
            VALUES (?, ?, ?, ?, 'pendente')
        """, (desejo.usuario_id, desejo.titulo, desejo.valor, desejo.link or ""))
        conn.commit()
        desejo.id = cursor.lastrowid
        return desejo

@app.post("/api/desejos/{desejo_id}/analisar")
def api_analisar_desejo(desejo_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        desejo = cursor.execute("SELECT * FROM lista_desejos WHERE id = ? AND usuario_id = ?", (desejo_id, usuario_id)).fetchone()
        if not desejo: raise HTTPException(status_code=404, detail="Item não encontrado.")
        transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (usuario_id,)).fetchall()
        entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
        saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
        saldo = entradas - saidas
        restante = saldo - desejo["valor"]
        return {"parecer": f"Comprando '{desejo['titulo']}' (R$ {desejo['valor']:.2f}), o seu saldo passará a ser R$ {restante:.2f}."}

@app.delete("/api/desejos/{desejo_id}")
def deletar_desejo(desejo_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM lista_desejos WHERE id = ? AND usuario_id = ?", (desejo_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.get("/api/insights")
def gerar_insights(usuario_id: int = Query(...)):
    """Sugestões financeiras objetivas com base nas transações reais do usuário."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT descricao, valor, tipo, categoria, data FROM transacoes WHERE usuario_id = ?",
            (usuario_id,)
        ).fetchall()
    transacoes = [dict(r) for r in rows]
    saidas = [t for t in transacoes if t["tipo"] == "saida"]
    entradas = sum(float(t["valor"] or 0) for t in transacoes if t["tipo"] == "entrada")
    total_saidas = sum(float(t["valor"] or 0) for t in saidas)
    saldo = entradas - total_saidas
    por_cat = {}
    for t in saidas:
        cat = t["categoria"] or "Outros"
        por_cat[cat] = por_cat.get(cat, 0) + float(t["valor"] or 0)
    insights = []
    if not transacoes:
        insights.append({"tipo":"info","titulo":"Comece registrando suas finanças","mensagem":"Adicione suas entradas e despesas para receber recomendações personalizadas. Você também pode dizer à IA: “gastei R$ 45 no mercado”."})
    else:
        if entradas > 0:
            taxa = total_saidas / entradas * 100
            if taxa >= 90:
                insights.append({"tipo":"alerta","titulo":"Quase toda a renda já está comprometida","mensagem":f"Suas saídas registradas equivalem a {taxa:.0f}% das entradas. Revise gastos variáveis e priorize as contas essenciais."})
            elif taxa >= 70:
                insights.append({"tipo":"atencao","titulo":"Atenção à proporção dos gastos","mensagem":f"Você já usou {taxa:.0f}% das entradas registradas em despesas. Tente reservar uma parte para imprevistos."})
            else:
                insights.append({"tipo":"positivo","titulo":"Acompanhe seu ritmo de gastos","mensagem":f"As saídas representam {taxa:.0f}% das entradas registradas. Continue conferindo os lançamentos para manter esse controle."})
        if por_cat:
            cat, valor = max(por_cat.items(), key=lambda item:item[1])
            percentual = (valor / total_saidas * 100) if total_saidas else 0
            insights.append({"tipo":"atencao","titulo":f"Categoria com maior gasto: {cat}","mensagem":f"Você registrou {valor:.2f} nessa categoria ({percentual:.0f}% das saídas). Veja se há despesas recorrentes que podem ser reduzidas."})
        if saldo < 0:
            insights.append({"tipo":"alerta","titulo":"Seu saldo registrado está negativo","mensagem":f"A diferença entre entradas e saídas é de R$ {saldo:.2f}. Evite novas despesas não essenciais até revisar o orçamento."})
        if len(saidas) >= 3:
            recentes = sorted(saidas, key=lambda t: t["data"] or "", reverse=True)[:10]
            media = sum(float(t["valor"] or 0) for t in recentes) / len(recentes)
            insights.append({"tipo":"info","titulo":"Confira pequenas despesas frequentes","mensagem":f"A média das últimas {len(recentes)} despesas registradas é R$ {media:.2f}. Pequenos gastos repetidos podem acumular; confira descrições e datas."})
    return {"saldo": round(saldo,2), "entradas": round(entradas,2), "saidas": round(total_saidas,2), "insights": insights[:5]}


def _data_transacao_ia(dados: dict, pergunta: str) -> str:
    """Usa a data de São Paulo; só aceita ano antigo quando o usuário o especifica."""
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    texto = (pergunta or "").lower()
    if "anteontem" in texto:
        return (hoje - timedelta(days=2)).isoformat()
    if "ontem" in texto:
        return (hoje - timedelta(days=1)).isoformat()
    # Sem ano explícito, não confiar na data alucinada pelo modelo (ex.: 2023).
    if not __import__("re").search(r"\b20\d{2}\b", texto):
        return hoje.isoformat()
    valor = str(dados.get("data") or "").strip()
    try:
        return date.fromisoformat(valor[:10]).isoformat()
    except (ValueError, TypeError):
        return hoje.isoformat()


@app.get("/api/health/ia")
def status_ia():
    providers = _ai_provider_order()
    return {
        "provedores_configurados": providers,
        "gemini_chave_configurada": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "gemini_sdk_instalado": HAS_GEMINI_SDK,
        "gemini_modelo": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "openai_chave_configurada": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "openai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "openai_modelo": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "ollama_modelo": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        "nota": "Este endpoint mostra configuração, não confirma quota ou conectividade do provedor."
    }


@app.post("/api/chat-ai")
def chat_mentor(payload: ChatPrompt):
    uid = payload.usuario_id
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (uid,)).fetchall()
            entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
            saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
            saldo = entradas - saidas

        resumo_categorias = {}
        for t in transacoes:
            if t["tipo"] == "saida":
                cat = t["categoria"] or "Outros"
                resumo_categorias[cat] = resumo_categorias.get(cat, 0) + float(t["valor"] or 0)
        resumo_txt = ", ".join(f"{k}: R$ {v:.2f}" for k, v in sorted(resumo_categorias.items(), key=lambda x:x[1], reverse=True))
        user_prompt = (f"Saldo registrado: R$ {saldo:.2f}. Entradas totais registradas: R$ {entradas:.2f}. "
            f"Saídas totais registradas: R$ {saidas:.2f}. Gastos por categoria: {resumo_txt or 'sem dados'}. "
            f"Data atual no fuso de São Paulo: {datetime.now(ZoneInfo('America/Sao_Paulo')).date().isoformat()}. "
            f"Para lançamentos sem data explícita, use a data atual; não use datas antigas como 2023. "
            f"Pedido do usuário: {payload.pergunta}")

        # Anexos visuais usam Gemini, se disponível. Sem Gemini, explica o limite sem fingir que leu o arquivo.
        if payload.ficheiro_base64:
            header, encoded = payload.ficheiro_base64.split(",", 1)
            import base64
            file_bytes = base64.b64decode(encoded)
            mime = payload.mime_type or "image/jpeg"
            if not os.getenv("GEMINI_API_KEY", "").strip() or not HAS_GEMINI_SDK:
                return {"resposta": "Para analisar fotos/PDF anexados, configure GEMINI_API_KEY. Para mensagens de texto, o sistema pode usar os outros provedores configurados.", "acao_executada": False}
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            part = types.Part.from_bytes(data=file_bytes, mime_type=mime) if "pdf" in mime else Image.open(io.BytesIO(file_bytes))
            response = client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), contents=[prompt_sistema, part, user_prompt])
            texto_limpo = getattr(response, "text", "") or ""
        else:
            texto_limpo, modelo_usado = _generate_text_with_provider(prompt_sistema, user_prompt)
        texto_limpo = _extract_response_text(texto_limpo)
        if not texto_limpo:
            raise ValueError("A IA não retornou conteúdo")
        # Alguns modelos acrescentam texto antes/depois do JSON; extrai o objeto.
        # Alguns modelos acrescentam texto antes/depois do JSON; extrai o objeto.
        inicio_json, fim_json = texto_limpo.find("{"), texto_limpo.rfind("}")
        if inicio_json < 0 or fim_json <= inicio_json:
            raise ValueError("Resposta Gemini não contém JSON válido")
        resultado = json.loads(texto_limpo[inicio_json:fim_json + 1])

        acao_executada = False
        resposta_final = resultado.get("resposta_texto", "Processado.")
        acao = resultado.get("acao")

        if acao == "adicionar_transacao":
            d = resultado.get("dados_transacao", {})
            valor_transacao = float(d.get("valor", 0.0))
            tipo_transacao = d.get("tipo", "saida")
            if valor_transacao <= 0 or tipo_transacao not in ("entrada", "saida"):
                return {"resposta": resposta_final or "Não consegui confirmar valor e tipo. Pode informar novamente?", "acao_executada": False}
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (uid, str(d.get("descricao", "Item"))[:180], valor_transacao, tipo_transacao, str(d.get("categoria", "Outros"))[:80], _data_transacao_ia(d, payload.pergunta)))
                conn.commit()
            acao_executada = True
        elif acao == "definir_orcamento":
            o = resultado.get("dados_orcamento", {})
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cat = o.get("categoria", "Outros")
                lim = float(o.get("limite_mensal", 0.0))
                existente = cursor.execute("SELECT id FROM orcamentos_categoria WHERE usuario_id = ? AND categoria = ?", (uid, cat)).fetchone()
                if existente:
                    cursor.execute("UPDATE orcamentos_categoria SET limite_mensal = ? WHERE id = ?", (lim, existente[0]))
                else:
                    cursor.execute("INSERT INTO orcamentos_categoria (usuario_id, categoria, limite_mensal) VALUES (?, ?, ?)", (uid, cat, lim))
                conn.commit()
            acao_executada = True

        return {"resposta": resposta_final, "acao_executada": acao_executada}
    except Exception as erro:
        import logging
        logging.exception("Falha no endpoint /api/chat-ai: %s", erro)
        return {"resposta": "A IA não conseguiu concluir agora. A chave pode estar configurada, mas o Render pode estar com modelo indisponível, limite de uso, chave sem permissão ou resposta inválida. Confira os logs do serviço no Render; tente novamente em instantes.", "acao_executada": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)