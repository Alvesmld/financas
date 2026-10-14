import os
import json
import io
import csv
import sqlite3
import hashlib
from pathlib import Path
from datetime import date, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
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
    title="Finanças Domésticas AI Engine Vision Sync",
    description="Backend com sincronização automática de mês/ano e IA Vision",
    version="23.0.0"
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

def buscar_transacoes_filtradas(usuario_id: int, d_inicio: str, d_fim: str, categoria: Optional[str] = None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if categoria:
            rows = cursor.execute("""
                SELECT * FROM transacoes 
                WHERE usuario_id = ? AND data BETWEEN ? AND ? AND categoria = ?
                ORDER BY data DESC, id DESC
            """, (usuario_id, d_inicio, d_fim, categoria)).fetchall()
        else:
            rows = cursor.execute("""
                SELECT * FROM transacoes 
                WHERE usuario_id = ? AND data BETWEEN ? AND ?
                ORDER BY data DESC, id DESC
            """, (usuario_id, d_inicio, d_fim)).fetchall()
        return [dict(r) for r in rows]

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

class AtualizarContaSchema(BaseModel):
    descricao: Optional[str] = None

class AtualizarStatusSchema(BaseModel):
    status: str

class MetaEconomiaSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    titulo: str
    valor_objetivo: float
    valor_atual: Optional[float] = 0.0
    data_limite: Optional[str] = None

class AtualizarMetaSchema(BaseModel):
    titulo: Optional[str] = None

class DesejoSchema(BaseModel):
    id: Optional[int] = None
    usuario_id: int
    titulo: str
    valor: float
    link: Optional[str] = ""
    status: Optional[str] = "pendente"

class AtualizarDesejoSchema(BaseModel):
    titulo: Optional[str] = None

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

@app.post("/api/scanner-contas-foto")
async def scanner_contas_foto(
    usuario_id: int = Form(...),
    foto: UploadFile = File(...)
):
    if not HAS_GEMINI_SDK:
        raise HTTPException(status_code=400, detail="SDK Gemini não configurado ou chave em falta.")

    try:
        conteudo_imagem = await foto.read()
        imagem = Image.open(io.BytesIO(conteudo_imagem))

        client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt_vision = """
        Analise esta imagem que contém várias contas listadas em fileiras (linhas).
        1. Identifique o MÊS e o ANO impressos ou escritos na folha/caderno (ex: "Janeiro/2026", "Outubro de 2026", etc.).
        2. Para cada conta detetada na fileira, extraia:
           - descricao: O nome da conta ou serviço.
           - valor: O valor numérico (float).
           - categoria: Escolha obrigatoriamente entre ['Moradia', 'Alimentação', 'Transporte', 'Lazer', 'Outros'].
           - status: 'paga' se ao lado da conta houver a sigla "PG" visível que esteja riscada, tachada ou com um traço por cima; caso contrário, defina 'pendente'.
           - data_vencimento: A data de vencimento formatada rigorosamente no padrão ISO "YYYY-MM-DD", sincronizando o dia da conta com o MÊS e ANO detetados na imagem. Se não houver dia específico, utilize o dia 10 do mês detetado.

        Retorne APENAS um JSON estrito com o seguinte formato, sem texto adicional:
        {
          "mes_ano_detectado": "Outubro/2026",
          "contas": [
            {"descricao": "...", "valor": 0.00, "categoria": "...", "status": "pendente"|"paga", "data_vencimento": "YYYY-MM-DD"}
          ]
        }
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[imagem, prompt_vision]
        )

        texto_limpo = response.text.replace("```json", "").replace("```", "").strip()
        dados_ia = json.loads(texto_limpo)

        mes_ano_detectado = dados_ia.get("mes_ano_detectado", "Mês Atual")
        contas_detectadas = dados_ia.get("contas", [])

        total_importadas = 0

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            for c in contas_detectadas:
                cursor.execute("""
                    INSERT INTO contas_a_pagar (usuario_id, descricao, valor, data_vencimento, categoria, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    usuario_id, 
                    c.get("descricao", "Conta detetada"), 
                    float(c.get("valor", 0.0)), 
                    c.get("data_vencimento", str(date.today())), 
                    c.get("categoria", "Outros"), 
                    c.get("status", "pendente")
                ))
                total_importadas += 1
            conn.commit()

        return {"sucesso": True, "total_importadas": total_importadas, "mes_ano_detectado": mes_ano_detectado, "itens": contas_detectadas}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar imagem com IA: {str(e)}")

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

@app.put("/api/contas/{conta_id}", response_model=ContaPagarSchema)
def atualizar_conta(conta_id: int, payload: AtualizarContaSchema, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        conta = cursor.execute("SELECT * FROM contas_a_pagar WHERE id = ? AND usuario_id = ?", (conta_id, usuario_id)).fetchone()
        if not conta: raise HTTPException(status_code=404, detail="Conta não encontrada.")
        
        nova_desc = payload.descricao if payload.descricao is not None else conta["descricao"]
        cursor.execute("UPDATE contas_a_pagar SET descricao = ? WHERE id = ?", (nova_desc, conta_id))
        conn.commit()
        conta_atualizada = cursor.execute("SELECT * FROM contas_a_pagar WHERE id = ?", (conta_id,)).fetchone()
        return dict(conta_atualizada)

@app.put("/api/contas/{conta_id}/status")
def alterar_status_conta(conta_id: int, payload: AtualizarStatusSchema, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        conta = cursor.execute("SELECT * FROM contas_a_pagar WHERE id = ? AND usuario_id = ?", (conta_id, usuario_id)).fetchone()
        if not conta: raise HTTPException(status_code=404, detail="Conta não encontrada.")
            
        cursor.execute("UPDATE contas_a_pagar SET status = ? WHERE id = ?", (payload.status, conta_id))
        
        if payload.status == 'paga':
            cursor.execute("""
                INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data)
                VALUES (?, ?, ?, 'saida', ?, ?)
            """, (usuario_id, f"Pagamento: {conta['descricao']}", conta["valor"], conta["categoria"], str(date.today())))
        
        conn.commit()
        return {"sucesso": True, "novo_status": payload.status}

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

@app.put("/api/metas/{meta_id}", response_model=MetaEconomiaSchema)
def atualizar_meta(meta_id: int, payload: AtualizarMetaSchema, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        meta = cursor.execute("SELECT * FROM metas_economia WHERE id = ? AND usuario_id = ?", (meta_id, usuario_id)).fetchone()
        if not meta: raise HTTPException(status_code=404, detail="Meta não encontrada.")
        
        novo_titulo = payload.titulo if payload.titulo is not None else meta["titulo"]
        cursor.execute("UPDATE metas_economia SET titulo = ? WHERE id = ?", (novo_titulo, meta_id))
        conn.commit()
        meta_atualizada = cursor.execute("SELECT * FROM metas_economia WHERE id = ?", (meta_id,)).fetchone()
        return dict(meta_atualizada)

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

@app.put("/api/desejos/{desejo_id}", response_model=DesejoSchema)
def atualizar_desejo(desejo_id: int, payload: AtualizarDesejoSchema, usuario_id: int = Query(...)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        desejo = cursor.execute("SELECT * FROM lista_desejos WHERE id = ? AND usuario_id = ?", (desejo_id, usuario_id)).fetchone()
        if not desejo: raise HTTPException(status_code=404, detail="Item não encontrado.")
        
        novo_titulo = payload.titulo if payload.titulo is not None else desejo["titulo"]
        cursor.execute("UPDATE lista_desejos SET titulo = ? WHERE id = ?", (novo_titulo, desejo_id))
        conn.commit()
        desejo_atualizado = cursor.execute("SELECT * FROM lista_desejos WHERE id = ?", (desejo_id,)).fetchone()
        return dict(desejo_atualizado)

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