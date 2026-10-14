import os
import json
import io
import sqlite3
import hashlib
import secrets
from pathlib import Path
from datetime import date, timedelta
from typing import List, Optional
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from PIL import Image

from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SENHA_FAMILIAR_MESTRA = os.getenv("SENHA_FAMILIAR_MESTRA", "chave_mestra_padrao_segura")

try:
    from google import genai
    from google.genai import types
    HAS_GEMINI_SDK = True if GEMINI_API_KEY else False
except ImportError:
    HAS_GEMINI_SDK = False

app = FastAPI(
    title="Finanças Domésticas AI - ALVESMLD SaaS Master",
    description="Backend corrigido com ano atual 2026 e tratamento de cota de IA",
    version="41.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "financas.db"
BASE_DIR = Path(__file__).resolve().parent

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
                senha_hash TEXT,
                sessao_token TEXT
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
        conn.commit()

init_db()

def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

class UsuarioLogin(BaseModel):
    email: str
    senha: str

class PrimeiroAcessoSchema(BaseModel):
    email: str
    nova_senha: str

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
        
        if credenciais.senha == SENHA_FAMILIAR_MESTRA:
            u = cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
            if not u:
                cursor.execute("""
                    INSERT INTO usuarios (nome, email, senha_hash) VALUES (?, ?, ?)
                """, ("Administrador / Mestre", email_limpo, hash_senha(credenciais.senha)))
                conn.commit()
                u = cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
        else:
            u = cursor.execute("SELECT * FROM usuarios WHERE email = ? AND senha_hash = ?", 
                               (email_limpo, hash_senha(credenciais.senha))).fetchone()
        
        if not u:
            raise HTTPException(status_code=401, detail="Acesso negado. E-mail não encontrado, senha incorreta ou pagamento não aprovado.")
        
        novo_token = secrets.token_hex(32)
        cursor.execute("UPDATE usuarios SET sessao_token = ? WHERE id = ?", (novo_token, u["id"]))
        conn.commit()
        
        return {
            "sucesso": True, 
            "usuario": {"id": u["id"], "nome": u["nome"], "email": u["email"]},
            "sessao_token": novo_token
        }

@app.post("/api/primeiro-acceso")
def primeiro_acesso(dados: PrimeiroAcessoSchema):
    email_limpo = dados.email.lower().strip()
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        u = cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,)).fetchone()
        
        if not u:
            raise HTTPException(status_code=404, detail="E-mail não encontrado como comprador na Cakto. Certifique-se de usar o mesmo e-mail da compra.")
        
        if u["senha_hash"] and u["senha_hash"] != "" and u["senha_hash"] != hash_senha(SENHA_FAMILIAR_MESTRA):
            raise HTTPException(status_code=400, detail="Esta conta já possui uma senha definida. Utilize a tela de login.")
        
        senha_h = hash_senha(dados.nova_senha)
        cursor.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (senha_h, u["id"]))
        conn.commit()
        
        return {"sucesso": True, "mensagem": "Senha definida com sucesso! Já pode fazer login."}

@app.post("/api/verificar-sessao")
def verificar_sessao(usuario_id: int = Query(...), token: str = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        u = cursor.execute("SELECT sessao_token FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if not u or u["sessao_token"] != token:
            raise HTTPException(status_code=401, detail="Sessão encerrada.")
        return {"valido": True}

@app.post("/api/webhook/cakto")
async def webhook_cakto(request: Request):
    try:
        dados = await request.json()
        
        status_pagamento = (
            dados.get("status") or 
            dados.get("event") or 
            dados.get("data", {}).get("status") or 
            ""
        ).lower()
        
        cliente_dados = dados.get("data", {}).get("customer", {}) or dados.get("customer", {})
        email_cliente = (cliente_dados.get("email") or dados.get("email") or "").lower().strip()
        nome_cliente = cliente_dados.get("name") or dados.get("name") or "Cliente Pro"
        
        if any(keyword in status_pagamento for keyword in ["approved", "paid", "order.paid", "test", "sucesso"]) and email_cliente:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                existente = cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email_cliente,)).fetchone()
                
                if not existente:
                    cursor.execute("""
                        INSERT INTO usuarios (nome, email, senha_hash) VALUES (?, ?, '')
                    """, (nome_cliente, email_cliente))
                    conn.commit()
                    acao = "comprador_registrado_para_primeiro_acesso"
                else:
                    acao = "cliente_ja_registrado"
                    
            return {"status": "sucesso", "acao": acao, "email": email_cliente}
            
        return {"status": "ignorado", "recebido": status_pagamento}
    except Exception as e:
        return {"erro": str(e)}

@app.post("/api/scanner-contas-foto")
async def scanner_contas_foto(
    usuario_id: int = Form(...),
    foto: UploadFile = File(...)
):
    if not HAS_GEMINI_SDK:
        raise HTTPException(status_code=400, detail="SDK Gemini não configurado.")

    try:
        conteudo_ficheiro = await foto.read()
        mime = foto.content_type or "image/jpeg"
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        if "pdf" in mime:
            part_file = types.Part.from_bytes(data=conteudo_ficheiro, mime_type="application/pdf")
        else:
            part_file = Image.open(io.BytesIO(conteudo_ficheiro))

        prompt_vision = f"""
        Analise este documento ou imagem. O ano atual obrigatório é 2026. Nunca utilize anos antigos como 2023.
        Retorne APENAS um JSON estrito:
        {{
          "mes_ano_detectado": "Outubro/2026",
          "contas": [{{"descricao": "...", "valor": 0.0, "categoria": "Outros", "status": "pendente", "data_vencimento": "2026-10-20"}}]
        }}
        """
        response = client.models.generate_content(model='gemini-2.5-flash', contents=[part_file, prompt_vision])
        texto_limpo = response.text.replace("```json", "").replace("```", "").strip()
        dados_ia = json.loads(texto_limpo)
        
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

@app.post("/api/chat-ai")
def chat_mentor(payload: ChatPrompt):
    uid = payload.usuario_id
    if not HAS_GEMINI_SDK:
        return {"resposta": "Chave Gemini não configurada.", "acao_executada": False, "progresso": 100}

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (uid,)).fetchall()
            entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
            saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
            saldo = entradas - saidas

        conteudo_prompt = [f"Saldo atual: R$ {saldo:.2f}. Data corrente obrigatória: 2026. Pedido em lote/linha: {payload.pergunta}"]
        
        if payload.ficheiro_base64:
            header, encoded = payload.ficheiro_base64.split(",", 1)
            import base64
            file_bytes = base64.b64decode(encoded)
            mime = payload.mime_type or "image/jpeg"
            if "pdf" in mime:
                conteudo_prompt.insert(0, types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"))
            else:
                conteudo_prompt.insert(0, Image.open(io.BytesIO(file_bytes)))

        prompt_sistema = """
        Você é o JARVIS, assistente financeiro inteligente, ágil e capaz de processar múltiplos comandos e listas em uma única linha de texto.
        REGRA CRUCIAL DE DATA: O ano atual obrigatório para todas as transações, vencimentos ou datas geradas é 2026. NUNCA utilize 2023 ou outros anos passados.
        Retorne APENAS um JSON estrito contendo uma lista de ações caso existam múltiplos pedidos, ou uma ação principal:
        {
          "resposta_texto": "...",
          "progresso_percentual": 100,
          "status_processamento": "Concluído com sucesso",
          "acoes": [
            {"acao": "adicionar_transacao", "dados_transacao": {"descricao": "...", "valor": 0.0, "tipo": "saida", "categoria": "Outros", "data": "2026-10-14"}},
            {"acao": "definir_orcamento", "dados_orcamento": {"categoria": "...", "limite_mensal": 0.0}}
          ]
        }
        """
        conteudo_prompt.insert(0, prompt_sistema)

        response = client.models.generate_content(model='gemini-2.5-flash', contents=conteudo_prompt)
        texto_limpo = response.text.replace("```json", "").replace("```", "").strip()
        resultado = json.loads(texto_limpo)

        acoes = resultado.get("acoes", [])
        resposta_final = resultado.get("resposta_texto", "Processado com sucesso.")
        progresso = resultado.get("progresso_percentual", 100)
        status_proc = resultado.get("status_processamento", "Concluído")

        acao_executada = False
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            for item in acoes:
                tipo_acao = item.get("acao")
                if tipo_acao == "adicionar_transacao":
                    d = item.get("dados_transacao", {})
                    cursor.execute("""
                        INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (uid, d.get("descricao", "Item"), float(d.get("valor", 0.0)), d.get("tipo", "saida"), d.get("categoria", "Outros"), d.get("data", str(date.today()))))
                    acao_executada = True
                elif tipo_acao == "definir_orcamento":
                    o = item.get("dados_orcamento", {})
                    cat = o.get("categoria", "Outros")
                    lim = float(o.get("limite_mensal", 0.0))
                    existente = cursor.execute("SELECT id FROM orcamentos_categoria WHERE usuario_id = ? AND categoria = ?", (uid, cat)).fetchone()
                    if existente:
                        cursor.execute("UPDATE orcamentos_categoria SET limite_mensal = ? WHERE id = ?", (lim, existente[0]))
                    else:
                        cursor.execute("INSERT INTO orcamentos_categoria (usuario_id, categoria, limite_mensal) VALUES (?, ?, ?)", (uid, cat, lim))
                    acao_executada = True
            conn.commit()

        return {
            "resposta": resposta_final, 
            "acao_executada": acao_executada if 'acao_executada' in locals() else True,
            "progresso": progresso,
            "status": status_proc
        }
    except Exception as e:
        erro_msg = str(e)
        if "429" in erro_msg or "RESOURCE_EXHAUSTED" in erro_msg:
            msg_amigavel = "Limite de requisições da IA atingido temporariamente (Cota excedida na API do Gemini). Tente novamente mais tarde."
        else:
            msg_amigavel = f"Erro ao processar o pedido: {erro_msg}"
            
        return {
            "resposta": msg_amigavel, 
            "acao_executada": False,
            "progresso": 0,
            "status": "Erro de cota ou comunicação"
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)