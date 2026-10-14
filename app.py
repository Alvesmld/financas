import os
import json
import io
import csv
import sqlite3
import hashlib
import base64
import urllib.parse
from pathlib import Path
from datetime import date, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

try:
    from google import genai
    HAS_GEMINI_SDK = True if GEMINI_API_KEY else False
except ImportError:
    HAS_GEMINI_SDK = False

app = FastAPI(
    title="Finanças Domésticas AI Engine Photo Support",
    description="Backend completo com suporte a imagens e rotas estáticas",
    version="18.0.0"
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
    return {"detail": "Ficheiro index.html não encontrado no servidor."}

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
                foto_base64 TEXT,
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

def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

class UsuarioCadastro(BaseModel):
    nome: str
    email: str
    senha: str

class UsuarioLogin(BaseModel):
    email: str
    senha: str

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

class DepositoMetaSchema(BaseModel):
    valor: float

class ChatPrompt(BaseModel):
    usuario_id: int
    pergunta: str

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
            raise HTTPException(status_code=400, detail="E-mail já registado.")

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

@app.get("/api/transacoes")
def listar_transacoes(usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        rows = cursor.execute("SELECT * FROM transacoes WHERE usuario_id = ? ORDER BY data DESC, id DESC", (usuario_id,)).fetchall()
        return [dict(r) for r in rows]

@app.post("/api/transacoes")
async def criar_transacao(
    usuario_id: int = Form(...),
    descricao: str = Form(...),
    valor: float = Form(...),
    tipo: str = Form(...),
    categoria: str = Form(...),
    data: str = Form(...),
    foto: Optional[UploadFile] = File(None)
):
    foto_b64 = None
    if foto:
        conteudo_foto = await foto.read()
        foto_b64 = base64.b64encode(conteudo_foto).decode('utf-8')

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data, foto_base64)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (usuario_id, descricao, valor, tipo, categoria, data, foto_b64))
        conn.commit()
        tid = cursor.lastrowid
        return {"sucesso": True, "id": tid}

@app.delete("/api/transacoes/{transacao_id}")
def deletar_transacao(transacao_id: int, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transacoes WHERE id = ? AND usuario_id = ?", (transacao_id, usuario_id))
        conn.commit()
        return {"sucesso": True}

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
        
        saldo_final = saldo_atual + (entradas * meses) - (saidas * meses) - total_contas
        
        return {
            "periodo_meses": meses,
            "saldo_atual": round(saldo_atual, 2),
            "entradas_estimadas": round(entradas * meses, 2),
            "saidas_estimadas": round(saidas * meses, 2),
            "contas_agendadas": round(total_contas, 2),
            "saldo_projetado_final": round(saldo_final, 2)
        }

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
        parecer = f"Comprando '{desejo['titulo']}' (R$ {desejo['valor']:.2f}), o seu saldo passará a ser R$ {restante:.2f}."
        return {"parecer": parecer}

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

@app.post("/api/chat-ai")
def chat_mentor(payload: ChatPrompt):
    return {"resposta": "Comando processado com sucesso.", "acao_executada": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)