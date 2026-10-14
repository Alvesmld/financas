import os
import json
import io
import csv
import sqlite3
import hashlib
import urllib.parse
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from PIL import Image

from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

try:
    from google import genai
    HAS_GEMINI_SDK = True if GEMINI_API_KEY else False
except ImportError:
    HAS_GEMINI_SDK = False

app = FastAPI(
    title="Finanças Domésticas AI Engine Auth Fix",
    description="Backend com CORS liberado e rota raiz servindo o frontend",
    version="17.0.0"
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

# --- ROTA RAIZ PARA SERVIR O FRONTEND ---

@app.get("/", include_in_schema=False)
def servir_frontend():
    index_file = BASE_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {"detail": "Ficheiro index.html não encontrado no servidor."}


# --- INICIALIZAÇÃO DO BANCO DE DADOS SQLITE ---

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL
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
        
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO usuarios (nome, email, senha_hash)
                VALUES ('Usuário Convidado', 'demo@financas.com', ?)
            """, (hashlib.sha256('123456'.encode()).hexdigest(),))
            
        conn.commit()

init_db()

def gerar_links_lojas(termo: str) -> Dict[str, str]:
    q = urllib.parse.quote(termo)
    return {
        "mercado_livre": f"https://lista.mercadolivre.com.br/{q}",
        "shopee": f"https://shopee.com.br/search?keyword={q}",
        "amazon": f"https://www.amazon.com.br/s?k={q}",
        "magalu": f"https://www.magazineluiza.com.br/busca/{q}/"
    }

def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

# --- SCHEMAS PYDANTIC ---

class UsuarioCadastro(BaseModel):
    nome: str
    email: str
    senha: str

class UsuarioLogin(BaseModel):
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

class ContaPagarSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    descricao: str
    valor: float
    data_vencimento: str
    categoria: str
    status: Optional[str] = "pendente"

class MetaEconomiaSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    titulo: str
    valor_objetivo: float
    valor_atual: Optional[float] = 0.0
    data_limite: Optional[str] = None

class DesejoSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    titulo: str
    valor: float
    link: Optional[str] = ""
    status: Optional[str] = "pendente"
    lojas: Optional[Dict[str, str]] = None

class DepositoMetaSchema(BaseModel):
    valor: float

class ChatPrompt(BaseModel):
    usuario_id: int
    pergunta: str


# --- AUTENTICAÇÃO ---

@app.post("/api/cadastrar")
def cadastrar_usuario(usuario: UsuarioCadastro):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO usuarios (nome, email, senha_hash)
                VALUES (?, ?, ?)
            """, (usuario.nome, usuario.email.lower().strip(), hash_senha(usuario.senha)))
            conn.commit()
            uid = cursor.lastrowid
            return {"sucesso": True, "usuario": {"id": uid, "nome": usuario.nome, "email": usuario.email}}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="E-mail já cadastrado.")

@app.post("/api/login")
def login_usuario(credenciais: UsuarioLogin):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        u = cursor.execute("SELECT * FROM usuarios WHERE email = ? AND senha_hash = ?", 
                           (credenciais.email.lower().strip(), hash_senha(credenciais.senha))).fetchone()
        if not u:
            raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
        return {"sucesso": True, "usuario": {"id": u["id"], "nome": u["nome"], "email": u["email"]}}


def obter_resumo_contexto_db(usuario_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (usuario_id,)).fetchall()
        contas = cursor.execute("SELECT * FROM contas_a_pagar WHERE usuario_id = ? AND status != 'paga'", (usuario_id,)).fetchall()
        desejos = cursor.execute("SELECT * FROM lista_desejos WHERE usuario_id = ?", (usuario_id,)).fetchall()
        
        entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
        saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
        
        return f"""
        RESUMO FINANCEIRO (Hoje: {date.today()}):
        - Saldo Atual: R$ {entradas - saidas:.2f}
        - Contas Agendadas: {json.dumps([dict(c) for c in contas], ensure_ascii=False)}
        - Lista de Desejos: {json.dumps([dict(d) for d in desejos], ensure_ascii=False)}
        - Amostra de Lançamentos: {json.dumps([dict(t) for t in transacoes[:8]], ensure_ascii=False)}
        """

def buscar_transacoes_filtradas(usuario_id: int, data_inicio: str, data_fim: str, categoria: Optional[str] = None) -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = "SELECT * FROM transacoes WHERE usuario_id = ? AND data >= ? AND data <= ?"
        params = [usuario_id, data_inicio, data_fim]
        
        if categoria:
            query += " AND categoria = ?"
            params.append(categoria)
            
        query += " ORDER BY data ASC, id ASC"
        rows = cursor.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


# --- AGENTE IA GEMINI ---

def processar_comando_agente_ia(mensagem_usuario: str, usuario_id: int) -> Dict[str, Any]:
    contexto_db = obter_resumo_contexto_db(usuario_id)
    hoje = date.today()
    ano_atual = hoje.year
    mes_atual = hoje.month

    if HAS_GEMINI_SDK:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            prompt_sistema = f"""
            Você é um assistente financeiro pessoal conectado ao banco do usuário.
            {contexto_db}
            DATA HOJE: {hoje}

            Analise a intenção e retorne um JSON estrito:

            1. Para ADICIONAR ITEM À LISTA DE DESEJOS:
            {{"acao": "CRIAR_DESEJO", "titulo": "str", "valor": 0.00, "link": "str"|null, "resposta_texto": "str"}}

            2. Para GERAR PLANILHA OU FILTRAR PERÍODO:
            {{"acao": "GERAR_PLANILHA", "data_inicio": "YYYY-MM-DD", "data_fim": "YYYY-MM-DD", "categoria": "str"|null, "resposta_texto": "str"}}

            3. Para ADICIONAR TRANSACAO DIRETA:
            {{"acao": "CRIAR_TRANSACAO", "tipo": "entrada"|"saida", "descricao": "str", "valor": 0.00, "categoria": "Alimentação"|"Moradia"|"Transporte"|"Lazer"|"Renda"|"Outros", "resposta_texto": "str"}}

            4. Para AGENDAR CONTA A PAGAR:
            {{"acao": "CRIAR_CONTA_PAGAR", "descricao": "str", "valor": 0.00, "data_vencimento": "YYYY-MM-DD", "categoria": "Moradia"|"Alimentação"|"Transporte"|"Lazer"|"Outros", "resposta_texto": "str"}}

            5. OUTROS:
            {{"acao": "RESPONDER_DUVIDA", "resposta_texto": "str"}}
            """

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[prompt_sistema, f"MENSAGEM DO USUÁRIO: {mensagem_usuario}"]
            )

            texto_limpo = response.text.replace("```json", "").replace("```", "").strip()
            dados_ia = json.loads(texto_limpo)

            if dados_ia.get("acao") == "GERAR_PLANILHA":
                d_i = dados_ia.get("data_inicio", f"{ano_atual}-{mes_atual:02d}-01")
                d_f = dados_ia.get("data_fim", str(hoje))
                cat = dados_ia.get("categoria")
                itens = buscar_transacoes_filtradas(usuario_id, d_i, d_f, cat)
                return {
                    "resposta": f"{dados_ia['resposta_texto']} ({len(itens)} itens encontrados).",
                    "acao_executada": True,
                    "tipo_acao": "PLANILHA",
                    "data_inicio": d_i,
                    "data_fim": d_f,
                    "categoria": cat,
                    "itens": itens
                }

            elif dados_ia.get("acao") == "CRIAR_DESEJO":
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO lista_desejos (usuario_id, titulo, valor, link, status)
                        VALUES (?, ?, ?, ?, 'pendente')
                    """, (usuario_id, dados_ia["titulo"], float(dados_ia["valor"]), dados_ia.get("link", "")))
                    conn.commit()
                return {"resposta": dados_ia["resposta_texto"], "acao_executada": True}

            elif dados_ia.get("acao") == "CRIAR_TRANSACAO":
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (usuario_id, dados_ia["descricao"], float(dados_ia["valor"]), dados_ia["tipo"], dados_ia["categoria"], str(hoje)))
                    conn.commit()
                return {"resposta": dados_ia["resposta_texto"], "acao_executada": True}

            elif dados_ia.get("acao") == "CRIAR_CONTA_PAGAR":
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO contas_a_pagar (usuario_id, descricao, valor, data_vencimento, categoria, status)
                        VALUES (?, ?, ?, ?, ?, 'pendente')
                    """, (usuario_id, dados_ia["descricao"], float(dados_ia["valor"]), dados_ia.get("data_vencimento", str(hoje)), dados_ia.get("categoria", "Moradia")))
                    conn.commit()
                return {"resposta": dados_ia["resposta_texto"], "acao_executada": True}

            else:
                return {"resposta": dados_ia.get("resposta_texto", "Comando processado."), "acao_executada": False}

        except Exception as e:
            print(f"Erro Gemini: {e}")

    return {"resposta": "Posso gerenciar seus gastos, lista de desejos e relatórios.", "acao_executada": False}


# --- ANÁLISE DE DESEJOS ---

def analisar_viabilidade_desejo(desejo_id: int, usuario_id: int) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        desejo = cursor.execute("SELECT * FROM lista_desejos WHERE id = ? AND usuario_id = ?", (desejo_id, usuario_id)).fetchone()
        if not desejo:
            raise HTTPException(status_code=404, detail="Item não encontrado.")
            
        transacoes = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ?", (usuario_id,)).fetchall()
        contas = cursor.execute("SELECT * FROM contas_a_pagar WHERE usuario_id = ? AND status != 'paga'", (usuario_id,)).fetchall()
        
        entradas = sum(t["valor"] for t in transacoes if t["tipo"] == "entrada")
        saidas = sum(t["valor"] for t in transacoes if t["tipo"] == "saida")
        saldo_atual = entradas - saidas
        total_contas = sum(c["valor"] for c in contas)
        
        valor_desejo = desejo["valor"]
        saldo_restante = saldo_atual - valor_desejo
        saldo_pos_contas = saldo_restante - total_contas
        links_lojas = gerar_links_lojas(desejo["titulo"])

        status_v = "viavel" if saldo_pos_contas > 200 else ("ressalva" if saldo_restante > 0 else "risco")
        parecer = f"Comprando '{desejo['titulo']}' (R$ {valor_desejo:.2f}), seu saldo ficará em R$ {saldo_restante:.2f}."
        
        return {
            "desejo": dict(desejo),
            "status_viabilidade": status_v,
            "parecer": parecer,
            "saldo_pos_compra": round(saldo_restante, 2),
            "saldo_pos_contas": round(saldo_pos_contas, 2),
            "lojas": links_lojas
        }


# --- ENDPOINTS REST ISOLADOS POR USUÁRIO ---

@app.get("/api/desejos", response_model=List[DesejoSchema])
def listar_desejos(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM lista_desejos WHERE usuario_id = ?", (usuario_id,)).fetchall()
        resultados = []
        for r in rows:
            item = dict(r)
            item["lojas"] = gerar_links_lojas(item["titulo"])
            resultados.append(item)
        return resultados

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
        desejo.lojas = gerar_links_lojas(desejo.titulo)
        return desejo

@app.post("/api/desejos/{desejo_id}/analisar")
def api_analisar_desejo(desejo_id: int, usuario_id: int = Query(...)):
    return analisar_viabilidade_desejo(desejo_id, usuario_id)

@app.delete("/api/desejos/{desejo_id}")
def deletar_desejo(desejo_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM lista_desejos WHERE id = ? AND usuario_id = ?", (desejo_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.get("/api/exportar-csv")
def exportar_csv(usuario_id: int = Query(...), data_inicio: Optional[str] = Query(None), data_fim: Optional[str] = Query(None), categoria: Optional[str] = Query(None)):
    d_inicio = data_inicio or "2000-01-01"
    d_fim = data_fim or "2099-12-31"
    transacoes = buscar_transacoes_filtradas(usuario_id, d_inicio, d_fim, categoria)
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['ID', 'Data', 'Descrição', 'Tipo', 'Categoria', 'Valor (R$)'])
    for t in transacoes:
        writer.writerow([t['id'], t['data'], t['descricao'], t['tipo'], t['categoria'], f"{t['valor']:.2f}".replace('.', ',')])
        
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=planilha_financeira_{date.today()}.csv"}
    )

@app.get("/api/hoje")
def obter_dados_hoje(usuario_id: int = Query(...)):
    hoje_str = str(date.today())
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        transacoes_hoje = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ? AND data = ? ORDER BY id DESC", (usuario_id, hoje_str)).fetchall()
        entradas = sum(t["valor"] for t in transacoes_hoje if t["tipo"] == "entrada")
        saidas = sum(t["valor"] for t in transacoes_hoje if t["tipo"] == "saida")
        return {
            "data": hoje_str,
            "total_entradas": round(entradas, 2),
            "total_saidas": round(saidas, 2),
            "saldo_dia": round(entradas - saidas, 2),
            "transacoes": [dict(t) for t in transacoes_hoje]
        }

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
        
        m_entradas = entradas if entradas > 0 else 0.0
        m_saidas = saidas if saidas > 0 else 0.0
        saldo_final = saldo_atual + (m_entradas * meses) - (m_saidas * meses) - total_contas
        
        return {
            "periodo_meses": meses,
            "saldo_atual": round(saldo_atual, 2),
            "entradas_estimadas": round(m_entradas * meses, 2),
            "saidas_estimadas": round(m_saidas * meses, 2),
            "contas_agendadas": round(total_contas, 2),
            "saldo_projetado_final": round(saldo_final, 2)
        }

@app.get("/api/transacoes", response_model=List[TransacaoSchema])
def listar_transacoes(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ? ORDER BY data DESC, id DESC", (usuario_id,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/transacoes", response_model=TransacaoSchema)
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

@app.get("/api/contas", response_model=List[ContaPagarSchema])
def listar_contas(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM contas_a_pagar WHERE usuario_id = ? ORDER BY data_vencimento ASC", (usuario_id,)).fetchall()
        hoje = str(date.today())
        resultados = []
        for r in rows:
            c = dict(r)
            if c["status"] == "pendente":
                if c["data_vencimento"] < hoje: c["status"] = "atrasada"
                elif c["data_vencimento"] == hoje: c["status"] = "hoje"
            resultados.append(c)
        return resultados

@app.post("/api/contas", response_model=ContaPagarSchema)
def criar_conta(conta: ContaPagarSchema):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO contas_a_pagar (usuario_id, descricao, valor, data_vencimento, categoria, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (conta.usuario_id, conta.descricao, conta.valor, conta.data_vencimento, conta.categoria, conta.status))
        conn.commit()
        conta.id = cursor.lastrowid
        return conta

@app.put("/api/contas/{conta_id}/pagar")
def pagar_conta(conta_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        conta = cursor.execute("SELECT * FROM contas_a_pagar WHERE id = ? AND usuario_id = ?", (conta_id, usuario_id)).fetchone()
        if not conta: raise HTTPException(status_code=404, detail="Conta não encontrada.")
            
        cursor.execute("UPDATE contas_a_pagar SET status = 'paga' WHERE id = ?", (conta_id,))
        cursor.execute("""
            INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
            VALUES (?, ?, ?, 'saida', ?, ?)
        """, (usuario_id, f"Pagamento: {conta['descricao']}", conta["valor"], conta["categoria"], str(date.today())))
        conn.commit()
        return {"sucesso": True}

@app.delete("/api/contas/{conta_id}")
def deletar_conta(conta_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contas_a_pagar WHERE id = ? AND usuario_id = ?", (conta_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.get("/api/metas", response_model=List[MetaEconomiaSchema])
def listar_metas(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM metas_economia WHERE usuario_id = ?", (usuario_id,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/metas", response_model=MetaEconomiaSchema)
def criar_meta(meta: MetaEconomiaSchema):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO metas_economia (usuario_id, titulo, valor_objetivo, valor_atual, data_limite)
            VALUES (?, ?, ?, ?, ?)
        """, (meta.usuario_id, meta.titulo, meta.valor_objetivo, meta.valor_atual or 0.0, meta.data_limite))
        conn.commit()
        meta.id = cursor.lastrowid
        return meta

@app.put("/api/metas/{meta_id}/depositar")
def depositar_meta(meta_id: int, payload: DepositoMetaSchema, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        meta = cursor.execute("SELECT * FROM metas_economia WHERE id = ? AND usuario_id = ?", (meta_id, usuario_id)).fetchone()
        if not meta: raise HTTPException(status_code=404, detail="Meta não encontrada.")
        
        novo_valor = meta["valor_atual"] + payload.valor
        cursor.execute("UPDATE metas_economia SET valor_atual = ? WHERE id = ?", (novo_valor, meta_id))
        cursor.execute("""
            INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
            VALUES (?, ?, ?, 'saida', 'Outros', ?)
        """, (usuario_id, f"Caixinha: {meta['titulo']}", payload.valor, str(date.today())))
        conn.commit()
        return {"sucesso": True, "novo_total": novo_valor}

@app.delete("/api/metas/{meta_id}")
def deletar_meta(meta_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM metas_economia WHERE id = ? AND usuario_id = ?", (meta_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

@app.post("/api/chat-ai")
def chat_mentor(payload: ChatPrompt):
    res = processar_comando_agente_ia(payload.pergunta, usuario_id=payload.usuario_id)
    return {
        "resposta": res["resposta"],
        "acao_executada": res.get("acao_executada", False),
        "tipo_acao": res.get("tipo_acao"),
        "data_inicio": res.get("data_inicio"),
        "data_fim": res.get("data_fim"),
        "categoria": res.get("categoria"),
        "itens": res.get("itens", [])
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)