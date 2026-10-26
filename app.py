import os
import json
import re
import base64
import uuid
import traceback
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.utils import secure_filename

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_ROOT, "uploads_tmp")
ALLOWED_EXTENSIONS = {"pdf"}
MAX_CONTENT_LENGTH = 15 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

DB_PATH = os.path.join(APP_ROOT, "financeiro.db")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

class Pessoa(db.Model):
    __tablename__ = "PESSOAS"
    id = db.Column(db.Integer, primary_key=True)
    razao_social = db.Column(db.String(255), nullable=False)
    documento = db.Column(db.String(30), nullable=True)
    tipo = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="ATIVO", nullable=False)

class Classificacao(db.Model):
    __tablename__ = "CLASSIFICACAO"
    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(255), nullable=False)
    subcategoria = db.Column(db.String(255), nullable=True)
    tipo = db.Column(db.String(50), default="DESPESA", nullable=False)
    status = db.Column(db.String(20), default="ATIVO", nullable=False)

movimento_classificacao = db.Table(
    "MOVIMENTO_CLASSIFICACAO",
    db.Column("movimento_id", db.Integer, db.ForeignKey("MOVIMENTOCONTAS.id"), primary_key=True),
    db.Column("classificacao_id", db.Integer, db.ForeignKey("CLASSIFICACAO.id"), primary_key=True),
)

class MovimentoContas(db.Model):
    __tablename__ = "MOVIMENTOCONTAS"
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), default="APAGAR", nullable=False)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey("PESSOAS.id"), nullable=True)
    faturado_id = db.Column(db.Integer, db.ForeignKey("PESSOAS.id"), nullable=True)
    classificacao_id = db.Column(db.Integer, db.ForeignKey("CLASSIFICACAO.id"), nullable=True)
    numero_nota = db.Column(db.String(50), nullable=True)
    data_emissao = db.Column(db.String(20), nullable=True)
    valor_total = db.Column(db.Float, nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default="ATIVO", nullable=False)

    fornecedor = db.relationship("Pessoa", foreign_keys=[fornecedor_id])
    faturado = db.relationship("Pessoa", foreign_keys=[faturado_id])
    classificacao = db.relationship("Classificacao", foreign_keys=[classificacao_id])
    classificacoes = db.relationship("Classificacao", secondary=movimento_classificacao, backref="movimentos")
    parcelas = db.relationship("ParcelaContas", back_populates="movimento")

class ParcelaContas(db.Model):
    __tablename__ = "PARCELACONTAS"
    id = db.Column(db.Integer, primary_key=True)
    movimento_id = db.Column(db.Integer, db.ForeignKey("MOVIMENTOCONTAS.id"), nullable=False)
    numero_parcela = db.Column(db.Integer, nullable=False)
    identificacao_unica = db.Column(db.String(100), unique=True, nullable=False)
    data_vencimento = db.Column(db.String(20), nullable=False)
    valor_parcela = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="ATIVO", nullable=False)
    movimento = db.relationship("MovimentoContas", back_populates="parcelas")

with app.app_context():
    db.create_all()
    # Copia a classificação legada para a nova relação muitos-para-muitos sem apagar dados.
    db.session.execute(text("""
        INSERT OR IGNORE INTO MOVIMENTO_CLASSIFICACAO (movimento_id, classificacao_id)
        SELECT id, classificacao_id FROM MOVIMENTOCONTAS WHERE classificacao_id IS NOT NULL
    """))
    db.session.commit()

CATEGORIAS_DESPESA = {
    "INSUMOS AGRÍCOLAS": [
        "Sementes", "Fertilizantes", "Defensivos Agrícolas", "Corretivos"
    ],
    "MANUTENÇÃO E OPERAÇÃO": [
        "Combustíveis e Lubrificantes", "Peças", "Parafusos",
        "Componentes Mecânicos", "Manutenção de Máquinas e Equipamentos",
        "Pneus", "Filtros", "Correias", "Ferramentas e Utensílios"
    ],
    "RECURSOS HUMANOS": [
        "Mão de Obra Temporária", "Salários e Encargos"
    ],
    "SERVIÇOS OPERACIONAIS": [
        "Frete e Transporte", "Colheita Terceirizada",
        "Secagem e Armazenagem", "Pulverização e Aplicação"
    ],
    "INFRAESTRUTURA E UTILIDADES": [
        "Energia Elétrica", "Arrendamento de Terras",
        "Construções e Reformas", "Materiais de Construção"
    ],
    "ADMINISTRATIVAS": [
        "Honorários (Contábeis, Advocatícios, Agronômicos)",
        "Despesas Bancárias e Financeiras"
    ],
    "SEGUROS E PROTEÇÃO": [
        "Seguro Agrícola", "Seguro de Ativos", "Seguro Prestamista"
    ],
    "IMPOSTOS E TAXAS": [
        "ITR", "IPTU", "IPVA", "INCRA-CCIR"
    ],
    "INVESTIMENTOS": [
        "Aquisição de Máquinas e Implementos", "Aquisição de Veículos",
        "Aquisição de Imóveis", "Infraestrutura Rural"
    ],
}

CATEGORIAS_PRINCIPAIS = list(CATEGORIAS_DESPESA.keys())

# Em uma base recém-criada (comum após deploy em hospedagem com SQLite efêmero),
# disponibiliza categorias padrão para que o formulário manual não fique vazio.
with app.app_context():
    for descricao, subcategorias in CATEGORIAS_DESPESA.items():
        for subcategoria in subcategorias:
            existente = Classificacao.query.filter_by(
                descricao=descricao, subcategoria=subcategoria, tipo="DESPESA"
            ).first()
            if existente is None:
                db.session.add(Classificacao(
                    descricao=descricao, subcategoria=subcategoria,
                    tipo="DESPESA", status="ATIVO"
                ))
    if not Classificacao.query.filter_by(tipo="RECEITA").first():
        db.session.add(Classificacao(
            descricao="RECEITAS OPERACIONAIS", subcategoria="Vendas e serviços",
            tipo="RECEITA", status="ATIVO"
        ))
    db.session.commit()

def montar_prompt_extracao() -> str:
    lista_categorias_formatada = "\n".join(
        f"- {categoria_principal}: {', '.join(subitens)}"
        for categoria_principal, subitens in CATEGORIAS_DESPESA.items()
    )

    return f"""
Você é um assistente especializado em extração de dados de Notas Fiscais / Faturas brasileiras em PDF, para uso em um sistema administrativo-financeiro do agronegócio.

Analise o documento PDF fornecido e extraia EXATAMENTE os dados abaixo em um ÚNICO objeto JSON válido, SEM markdown (não use ```).

ESTRUTURA JSON OBRIGATÓRIA:
{{
  "fornecedor": {{
    "razao_social": string ou null,
    "nome_fantasia": string ou null,
    "cnpj": string ou null
  }},
  "faturado": {{
    "nome_completo": string ou null,
    "cpf": string ou null
  }},
  "nota_fiscal": {{
    "numero": string ou null,
    "data_emissao": string ou null (formato DD/MM/AAAA)
  }},
  "descricao_produtos": string ou null,
  "valor_total": number ou null,
  "quantidade_parcelas": integer,
  "parcelas": [
    {{
      "numero_parcela": integer,
      "data_vencimento": string (formato DD/MM/AAAA),
      "valor_parcela": number
    }}
  ],
  "classificacao_despesas": [
    {{
      "categoria_principal": string,
      "subcategoria": string,
      "justificativa": string
    }}
  ],
  "observacoes": string ou null
}}

CATEGORIAS PRINCIPAIS E SUBCATEGORIAS VÁLIDAS:
{lista_categorias_formatada}
""".strip()

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def limpar_mensagem_de_erro(mensagem: str) -> str:
    if not mensagem:
        return ""
    mensagem = re.sub(r"(key=)[^\s&\"'\\]+", r"\1***REMOVIDA***", mensagem)
    mensagem = re.sub(r"\bAIza[0-9A-Za-z\-_]{10,}", "***REMOVIDA***", mensagem)
    return re.sub(r"\bAQ\.[0-9A-Za-z\-_\.]{10,}", "***REMOVIDA***", mensagem)

def traduzir_erro_gemini(erro: Exception) -> tuple:
    texto = limpar_mensagem_de_erro(str(erro))
    if "API_KEY_INVALID" in texto or "API key not valid" in texto:
        return ("A chave da API do Gemini foi rejeitada. Verifique a GEMINI_API_KEY.", 502)
    if "RESOURCE_EXHAUSTED" in texto or "quota" in texto.lower():
        return ("Limite de uso da API do Gemini atingido. Aguarde alguns minutos.", 429)
    return (f"Não foi possível concluir a extração: {texto}", 500)

def extrair_json_da_resposta(texto_resposta: str) -> dict:
    if not texto_resposta:
        raise ValueError("Resposta vazia do modelo Gemini.")
    texto = re.sub(r"^```(?:json)?\s*", "", texto_resposta.strip(), flags=re.IGNORECASE)
    texto = re.sub(r"\s*```$", "", texto).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        inicio = texto.find("{")
        fim = texto.rfind("}")
        if inicio != -1 and fim > inicio:
            return json.loads(texto[inicio : fim + 1])
        raise ValueError("Não foi possível localizar um JSON válido.")

def normalizar_documento(documento: str) -> str:
    return re.sub(r"\D", "", documento or "")

def normalizar_valor(valor, campo: str):
    """Aceita números JSON e valores monetários textuais usados por alguns modelos."""
    if isinstance(valor, bool) or valor is None:
        raise ValueError(f"{campo} não foi informado como um valor válido.")
    if isinstance(valor, (int, float, Decimal)):
        numero = Decimal(str(valor))
    elif isinstance(valor, str):
        texto = re.sub(r"[^0-9,.-]", "", valor.strip())
        if not texto:
            raise ValueError(f"{campo} não foi informado como um valor válido.")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            numero = Decimal(texto)
        except InvalidOperation as erro:
            raise ValueError(f"{campo} não foi informado como um valor válido.") from erro
    else:
        raise ValueError(f"{campo} não foi informado como um valor válido.")
    if not numero.is_finite() or numero <= 0:
        raise ValueError(f"{campo} deve ser maior que zero.")
    return float(numero)

def normalizar_data(data, campo: str):
    """Padroniza datas extraídas para DD/MM/AAAA e devolve erro de validação legível."""
    texto = str(data or "").strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(texto, formato).strftime("%d/%m/%Y")
        except ValueError:
            continue
    raise ValueError(f"{campo} está ausente ou inválida. Use DD/MM/AAAA.")

def buscar_pessoa_ativa(documento: str, nome: str, tipo: str):
    pessoas = Pessoa.query.filter_by(tipo=tipo, status="ATIVO").all()
    documento_normalizado = normalizar_documento(documento)
    if documento_normalizado:
        for pessoa in pessoas:
            if normalizar_documento(pessoa.documento) == documento_normalizado:
                return pessoa
    nome_normalizado = (nome or "").strip().casefold()
    if nome_normalizado:
        for pessoa in pessoas:
            if (pessoa.razao_social or "").strip().casefold() == nome_normalizado:
                return pessoa
    return None

def analisar_dados_no_banco(dados: dict) -> dict:
    relatorio_analise = {}

    forn_info = dados.get("fornecedor", {})
    cnpj = forn_info.get("cnpj")
    razao_social = forn_info.get("razao_social")

    fornecedor_db = buscar_pessoa_ativa(cnpj, razao_social, "CLIENTE-FORNECEDOR")

    if fornecedor_db:
        relatorio_analise["fornecedor"] = {
            "status": "EXISTE",
            "id": fornecedor_db.id,
            "razao_social": fornecedor_db.razao_social,
            "documento": fornecedor_db.documento,
            "mensagem": f"EXISTE – ID: {fornecedor_db.id}"
        }
    else:
        relatorio_analise["fornecedor"] = {
            "status": "NÃO EXISTE",
            "id": None,
            "razao_social": razao_social or "NÃO INFORMADO",
            "documento": cnpj or "NÃO INFORMADO",
            "mensagem": "NÃO EXISTE"
        }

    fat_info = dados.get("faturado", {})
    cpf = fat_info.get("cpf")
    nome_faturado = fat_info.get("nome_completo")

    faturado_db = buscar_pessoa_ativa(cpf, nome_faturado, "FATURADO")

    if faturado_db:
        relatorio_analise["faturado"] = {
            "status": "EXISTE",
            "id": faturado_db.id,
            "nome": faturado_db.razao_social,
            "documento": faturado_db.documento,
            "mensagem": f"EXISTE – ID: {faturado_db.id}"
        }
    else:
        relatorio_analise["faturado"] = {
            "status": "NÃO EXISTE",
            "id": None,
            "nome": nome_faturado or "NÃO INFORMADO",
            "documento": cpf or "NÃO INFORMADO",
            "mensagem": "NÃO EXISTE"
        }

    classificacoes_info = dados.get("classificacao_despesas", []) or []
    if not classificacoes_info:
        classificacoes_info = [{"categoria_principal": "ADMINISTRATIVAS", "subcategoria": None}]
    analise_classificacoes = []
    for item in classificacoes_info:
        descricao = (item.get("categoria_principal") or "ADMINISTRATIVAS").strip()
        subcategoria = (item.get("subcategoria") or "").strip() or None
        registro = Classificacao.query.filter_by(
            descricao=descricao, subcategoria=subcategoria, tipo="DESPESA", status="ATIVO"
        ).first()
        analise_classificacoes.append({
            "status": "EXISTE" if registro else "NÃO EXISTE",
            "id": registro.id if registro else None,
            "descricao": registro.descricao if registro else descricao,
            "subcategoria": registro.subcategoria if registro else subcategoria,
            "mensagem": f"EXISTE – ID: {registro.id}" if registro else "NÃO EXISTE",
        })
    relatorio_analise["despesas"] = analise_classificacoes
    relatorio_analise["despesa"] = analise_classificacoes[0]
    return relatorio_analise

@app.errorhandler(413)
def arquivo_grande_demais(_erro):
    return jsonify({"sucesso": False, "erro": "Arquivo muito grande. O limite é de 15 MB."}), 413

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/extrair-dados", methods=["POST"])
def extrair_dados():
    if not GEMINI_API_KEY:
        return jsonify({"sucesso": False, "erro": "GEMINI_API_KEY não configurada no servidor."}), 500

    if "arquivo" not in request.files or request.files["arquivo"].filename == "":
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo enviado."}), 400

    arquivo = request.files["arquivo"]
    if not allowed_file(arquivo.filename):
        return jsonify({"sucesso": False, "erro": "Formato inválido. Apenas arquivos PDF são aceitos."}), 400

    nome_seguro = secure_filename(arquivo.filename)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    caminho_temp = os.path.join(app.config["UPLOAD_FOLDER"], f"{timestamp}_{nome_seguro}")

    try:
        arquivo.save(caminho_temp)
        with open(caminho_temp, "rb") as f:
            bytes_pdf = f.read()

        documento_pdf = {
            "mime_type": "application/pdf",
            "data": base64.b64encode(bytes_pdf).decode("utf-8"),
        }

        modelo = genai.GenerativeModel(
            model_name=GEMINI_MODEL_NAME,
            generation_config={"temperature": 0.1, "response_mime_type": "application/json"},
            safety_settings={HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE},
        )

        resposta = modelo.generate_content([documento_pdf, montar_prompt_extracao()])
        dados_extraidos = extrair_json_da_resposta(resposta.text)
        if not isinstance(dados_extraidos, dict):
            return jsonify({"sucesso": False, "erro": "A resposta da extração não contém um objeto JSON."}), 422
        for chave in ("fornecedor", "faturado", "nota_fiscal"):
            if not isinstance(dados_extraidos.get(chave), dict):
                return jsonify({"sucesso": False, "erro": f"A extração não retornou o campo '{chave}' no formato esperado."}), 422
        if not isinstance(dados_extraidos.get("parcelas"), list) or not isinstance(dados_extraidos.get("classificacao_despesas"), list):
            return jsonify({"sucesso": False, "erro": "A extração não retornou parcelas e classificações em formato de lista."}), 422

        analise_banco = analisar_dados_no_banco(dados_extraidos)

        return jsonify({
            "sucesso": True,
            "dados": dados_extraidos,
            "analise_banco": analise_banco
        }), 200

    except Exception as erro:
        traceback.print_exc()
        mensagem_erro, status_erro = traduzir_erro_gemini(erro)
        return jsonify({"sucesso": False, "erro": mensagem_erro}), status_erro

    finally:
        if os.path.exists(caminho_temp):
            os.remove(caminho_temp)

@app.route("/api/lancar-movimento", methods=["POST"])
def lancar_movimento():
    try:
        payload = request.get_json()
        if not isinstance(payload, dict):
            return jsonify({"sucesso": False, "erro": "Corpo JSON inválido."}), 400
        dados = payload.get("dados")
        if not isinstance(dados, dict):
            return jsonify({"sucesso": False, "erro": "Dados para lançamento inválidos."}), 400

        fornecedor_info = dados.get("fornecedor") or {}
        faturado_info = dados.get("faturado") or {}
        parcelas_lista = dados.get("parcelas") or []
        nota_fiscal = dados.get("nota_fiscal") or {}
        if not all(isinstance(item, dict) for item in (fornecedor_info, faturado_info, nota_fiscal)):
            return jsonify({"sucesso": False, "erro": "Dados de fornecedor, faturado ou nota fiscal inválidos."}), 400
        if not isinstance(parcelas_lista, list) or not parcelas_lista:
            return jsonify({"sucesso": False, "erro": "A nota deve possuir ao menos uma parcela válida."}), 400
        nome_fornecedor = str(fornecedor_info.get("razao_social") or "").strip()
        nome_faturado = str(faturado_info.get("nome_completo") or "").strip()
        if not nome_fornecedor or not nome_faturado:
            return jsonify({"sucesso": False, "erro": "O lançamento não foi gravado: o PDF precisa informar o nome do fornecedor e do faturado. Reanalise o documento e confira a extração."}), 400
        try:
            valor_total = normalizar_valor(dados.get("valor_total"), "Valor total da nota")
            vencimentos = set()
            for indice, parcela in enumerate(parcelas_lista, 1):
                if not isinstance(parcela, dict):
                    raise ValueError(f"A parcela {indice} está em formato inválido.")
                parcela["data_vencimento"] = normalizar_data(parcela.get("data_vencimento"), f"Vencimento da parcela {indice}")
                parcela["valor_parcela"] = normalizar_valor(parcela.get("valor_parcela"), f"Valor da parcela {indice}")
                if parcela["data_vencimento"] in vencimentos:
                    raise ValueError("As datas de vencimento das parcelas devem ser distintas.")
                vencimentos.add(parcela["data_vencimento"])
        except ValueError as erro:
            return jsonify({"sucesso": False, "erro": f"O lançamento não foi gravado: {erro}"}), 400
        # Converte campos textuais opcionais para strings antes de persistir.
        fornecedor_info["razao_social"] = nome_fornecedor
        faturado_info["nome_completo"] = nome_faturado

        # Recalcula no servidor para não confiar na análise enviada pelo navegador.
        analise = analisar_dados_no_banco(dados)

        if analise["fornecedor"]["status"] == "NÃO EXISTE":
            novo_fornecedor = Pessoa(
                razao_social=fornecedor_info["razao_social"].strip(),
                documento=str(fornecedor_info.get("cnpj") or "").strip() or None,
                tipo="CLIENTE-FORNECEDOR",
                status="ATIVO"
            )
            db.session.add(novo_fornecedor)
            db.session.flush()
            fornecedor_id = novo_fornecedor.id
        else:
            fornecedor_id = analise["fornecedor"]["id"]

        if analise["faturado"]["status"] == "NÃO EXISTE":
            novo_faturado = Pessoa(
                razao_social=faturado_info["nome_completo"].strip(),
                documento=str(faturado_info.get("cpf") or "").strip() or None,
                tipo="FATURADO",
                status="ATIVO"
            )
            db.session.add(novo_faturado)
            db.session.flush()
            faturado_id = novo_faturado.id
        else:
            faturado_id = analise["faturado"]["id"]

        classificacoes_movimento = []
        for item in analise["despesas"]:
            classificacao = Classificacao.query.filter_by(
                descricao=item["descricao"], subcategoria=item["subcategoria"],
                tipo="DESPESA", status="ATIVO"
            ).first()
            if classificacao is None:
                classificacao = Classificacao(
                    descricao=item["descricao"], subcategoria=item["subcategoria"],
                    tipo="DESPESA", status="ATIVO"
                )
                db.session.add(classificacao)
                db.session.flush()
            if classificacao not in classificacoes_movimento:
                classificacoes_movimento.append(classificacao)
        despesa_id = classificacoes_movimento[0].id if classificacoes_movimento else None

        novo_movimento = MovimentoContas(
            tipo="APAGAR",
            fornecedor_id=fornecedor_id,
            faturado_id=faturado_id,
            classificacao_id=despesa_id,
            classificacoes=classificacoes_movimento,
            numero_nota=nota_fiscal.get("numero"),
            data_emissao=nota_fiscal.get("data_emissao"),
            valor_total=valor_total,
            descricao=dados.get("descricao_produtos"),
            status="ATIVO"
        )
        db.session.add(novo_movimento)
        db.session.flush()

        for p in parcelas_lista:
            nova_parcela = ParcelaContas(
                movimento_id=novo_movimento.id,
                numero_parcela=p.get("numero_parcela") or 1,
                identificacao_unica=f"PARC-{novo_movimento.id}-{p.get('numero_parcela') or 1}-{uuid.uuid4().hex}",
                data_vencimento=p.get("data_vencimento", ""),
                valor_parcela=p["valor_parcela"],
                status="ATIVO"
            )
            db.session.add(nova_parcela)

        db.session.commit()

        return jsonify({
            "sucesso": True,
            "mensagem": "REGISTRO E PARCELAS LANÇADOS COM SUCESSO!",
            "movimento_id": novo_movimento.id
        }), 201

    except Exception as e:
        db.session.rollback()
        traceback.print_exc()
        return jsonify({"sucesso": False, "erro": f"Erro ao gravar lançamento: {str(e)}"}), 500

def erro_validacao(mensagem, codigo=400):
    return jsonify({"sucesso": False, "erro": mensagem}), codigo

def validar_tipo_classificacao(tipo):
    tipo = (tipo or "").upper()
    if tipo not in {"DESPESA", "RECEITA"}:
        raise ValueError("Tipo de classificação deve ser DESPESA ou RECEITA.")
    return tipo

@app.route("/api/classificacoes", methods=["GET"])
def listar_classificacoes():
    status = request.args.get("status", "ATIVO").upper()
    tipo = request.args.get("tipo")
    busca = (request.args.get("busca") or "").strip()
    query = Classificacao.query.filter_by(status=status)
    if tipo:
        try:
            tipo = validar_tipo_classificacao(tipo)
        except ValueError as erro:
            return erro_validacao(str(erro))
        query = query.filter_by(tipo=tipo)
    if busca:
        termos = busca.split()
        for termo in termos:
            filtro = Classificacao.descricao.ilike(f"%{termo}%") | Classificacao.subcategoria.ilike(f"%{termo}%")
            query = query.filter(filtro)
    registros = query.order_by(Classificacao.descricao, Classificacao.subcategoria).all()
    return jsonify([{"id": x.id, "descricao": x.descricao, "subcategoria": x.subcategoria,
                     "tipo": x.tipo, "status": x.status} for x in registros])

@app.route("/api/classificacoes", methods=["POST"])
def criar_classificacao():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return erro_validacao("Corpo JSON inválido.")
    descricao = (dados.get("descricao") or "").strip()
    if not descricao:
        return erro_validacao("Descrição é obrigatória.")
    try:
        tipo = validar_tipo_classificacao(dados.get("tipo"))
    except ValueError as erro:
        return erro_validacao(str(erro))
    registro = Classificacao(descricao=descricao, subcategoria=(dados.get("subcategoria") or "").strip() or None,
                             tipo=tipo, status="ATIVO")
    db.session.add(registro)
    db.session.commit()
    return jsonify({"sucesso": True, "id": registro.id, "mensagem": "Classificação criada."}), 201

@app.route("/api/classificacoes/<int:registro_id>", methods=["PUT"])
def editar_classificacao(registro_id):
    registro = Classificacao.query.get_or_404(registro_id)
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return erro_validacao("Corpo JSON inválido.")
    descricao = (dados.get("descricao") or "").strip()
    if not descricao:
        return erro_validacao("Descrição é obrigatória.")
    try:
        tipo = validar_tipo_classificacao(dados.get("tipo"))
    except ValueError as erro:
        return erro_validacao(str(erro))
    registro.descricao = descricao
    registro.subcategoria = (dados.get("subcategoria") or "").strip() or None
    registro.tipo = tipo
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Classificação atualizada."})

@app.route("/api/classificacoes/<int:registro_id>/inativar", methods=["PUT"])
def inativar_classificacao(registro_id):
    registro = Classificacao.query.get_or_404(registro_id)
    registro.status = "INATIVO"
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Classificação inativada."})

@app.route("/api/classificacoes/<int:registro_id>/reativar", methods=["PUT"])
def reativar_classificacao(registro_id):
    registro = Classificacao.query.get_or_404(registro_id)
    registro.status = "ATIVO"
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Classificação reativada."})

@app.route("/api/movimentos", methods=["POST"])
def criar_movimento():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return erro_validacao("Corpo JSON inválido.")
    tipo = (dados.get("tipo") or "").upper()
    if tipo not in {"APAGAR", "RECEBER"}:
        return erro_validacao("Tipo deve ser APAGAR ou RECEBER.")
    valor_total = dados.get("valor_total")
    parcelas = dados.get("parcelas")
    if not isinstance(valor_total, (int, float)) or isinstance(valor_total, bool) or valor_total <= 0:
        return erro_validacao("Valor total inválido.")
    if not isinstance(parcelas, list) or not parcelas:
        return erro_validacao("Informe ao menos uma parcela.")
    vencimentos = []
    for i, parcela in enumerate(parcelas, 1):
        if not isinstance(parcela, dict) or not parcela.get("data_vencimento"):
            return erro_validacao(f"Informe vencimento para a parcela {i}.")
        if parcela["data_vencimento"] in vencimentos:
            return erro_validacao("As datas de vencimento das parcelas devem ser distintas.")
        vencimentos.append(parcela["data_vencimento"])
        if not isinstance(parcela.get("valor_parcela"), (int, float)) or isinstance(parcela.get("valor_parcela"), bool) or parcela["valor_parcela"] <= 0:
            return erro_validacao(f"Valor inválido na parcela {i}.")
    pessoa = Pessoa.query.filter_by(id=dados.get("pessoa_id"), status="ATIVO").first()
    if pessoa is None:
        return erro_validacao("Selecione uma pessoa ativa.")
    tipos_pessoa_permitidos = {"CLIENTE-FORNECEDOR", "FORNECEDOR"} if tipo == "APAGAR" else {"CLIENTE-FORNECEDOR", "CLIENTE"}
    if pessoa.tipo not in tipos_pessoa_permitidos:
        return erro_validacao("O tipo da pessoa selecionada não corresponde ao movimento.")
    classificacao_ids = dados.get("classificacao_ids") or []
    if not isinstance(classificacao_ids, list) or not classificacao_ids:
        return erro_validacao("Selecione ao menos uma classificação.")
    tipo_classificacao = "DESPESA" if tipo == "APAGAR" else "RECEITA"
    classificacoes = Classificacao.query.filter(
        Classificacao.id.in_(classificacao_ids), Classificacao.status == "ATIVO",
        Classificacao.tipo == tipo_classificacao
    ).all()
    if len({x.id for x in classificacoes}) != len(set(classificacao_ids)):
        return erro_validacao(f"Selecione classificações ativas do tipo {tipo_classificacao}.")
    movimento = MovimentoContas(
        tipo=tipo, fornecedor_id=pessoa.id if tipo == "APAGAR" else None,
        faturado_id=pessoa.id if tipo == "RECEBER" else None,
        classificacao_id=classificacoes[0].id, classificacoes=classificacoes,
        numero_nota=(dados.get("numero_nota") or "").strip() or None,
        data_emissao=(dados.get("data_emissao") or "").strip() or None,
        valor_total=valor_total, descricao=(dados.get("descricao") or "").strip() or None,
        status="ATIVO"
    )
    db.session.add(movimento)
    db.session.flush()
    for i, parcela in enumerate(parcelas, 1):
        db.session.add(ParcelaContas(
            movimento_id=movimento.id, numero_parcela=parcela.get("numero_parcela") or i,
            identificacao_unica=f"MOV-{movimento.id}-{uuid.uuid4().hex}",
            data_vencimento=parcela["data_vencimento"], valor_parcela=parcela["valor_parcela"], status="ATIVO"
        ))
    db.session.commit()
    return jsonify({"sucesso": True, "movimento_id": movimento.id, "mensagem": "Movimento lançado com sucesso."}), 201

@app.route("/api/pessoas", methods=["GET"])
def listar_pessoas():
    busca = request.args.get("busca")
    status = request.args.get("status", "ATIVO")

    query = Pessoa.query.filter_by(status=status)
    if busca:
        for termo in busca.split():
            query = query.filter(Pessoa.razao_social.ilike(f"%{termo}%") | Pessoa.documento.ilike(f"%{termo}%") | Pessoa.tipo.ilike(f"%{termo}%"))

    pessoas = query.all()
    return jsonify([{
        "id": p.id,
        "razao_social": p.razao_social,
        "documento": p.documento,
        "tipo": p.tipo,
        "status": p.status
    } for p in pessoas])

@app.route("/api/pessoas", methods=["POST"])
def criar_pessoa():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return erro_validacao("Corpo JSON inválido.")
    razao_social = (dados.get("razao_social") or "").strip()
    tipo = (dados.get("tipo") or "").strip().upper()
    tipos_validos = {"CLIENTE-FORNECEDOR", "FATURADO", "CLIENTE", "FORNECEDOR"}
    if not razao_social or tipo not in tipos_validos:
        return erro_validacao("Nome e tipo de pessoa válidos são obrigatórios.")
    pessoa = Pessoa(razao_social=razao_social, documento=(dados.get("documento") or "").strip() or None,
                    tipo=tipo, status="ATIVO")
    db.session.add(pessoa)
    db.session.commit()
    return jsonify({"sucesso": True, "id": pessoa.id, "mensagem": "Pessoa criada."}), 201

@app.route("/api/pessoas/<int:pessoa_id>", methods=["PUT"])
def editar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        return erro_validacao("Corpo JSON inválido.")
    razao_social = (dados.get("razao_social") or "").strip()
    tipo = (dados.get("tipo") or "").strip().upper()
    tipos_validos = {"CLIENTE-FORNECEDOR", "FATURADO", "CLIENTE", "FORNECEDOR"}
    if not razao_social or tipo not in tipos_validos:
        return erro_validacao("Nome e tipo de pessoa válidos são obrigatórios.")
    pessoa.razao_social = razao_social
    pessoa.documento = (dados.get("documento") or "").strip() or None
    pessoa.tipo = tipo
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Pessoa atualizada."})

@app.route("/api/pessoas/<int:pessoa_id>/inativar", methods=["PUT"])
def inativar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    pessoa.status = "INATIVO"
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Registro inativado com sucesso."})

@app.route("/api/pessoas/<int:pessoa_id>/reativar", methods=["PUT"])
def reativar_pessoa(pessoa_id):
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    pessoa.status = "ATIVO"
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": "Registro reativado com sucesso."})

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)