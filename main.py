import os
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, filters
)
import mercadopago
from aiohttp import web
import aiohttp

# Configuração
BOT_TOKEN = "8184530038:AAGlNXIBfgnVpJmPTIk2kx6KTRzZrkhe8kI"
MERCADOPAGO_TOKEN = "TEST-514306390150238-082415-412dfefa5af2206b2624600120cbff21-2465734771"
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://your-app.railway.app')
PORT = int(os.getenv('PORT', 8000))

# IDs dos grupos e canais (substitua com os seus)
CHANNEL_CONTOS_ID = -1001234567890
GROUP_INFO_ID = -1000987654321
GROUP_EROSVIP_ID = -1001122334455

# IDs dos administradores (seus ADMs)
ADMINS = [2134345469, 7392978394]  # IDs dos administradores

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Inicializar Mercado Pago
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)

# Inicializar banco de dados
def init_db():
    conn = sqlite3.connect('subscriptions.db')
    c = conn.cursor()
    
    # Tabela de usuários
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, 
                  date_joined DATETIME)''')
    
    # Tabela de assinaturas
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  plan TEXT,
                  start_date DATETIME,
                  expiration_date DATETIME,
                  active BOOLEAN,
                  payment_id TEXT,
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    # Tabela com links de convite únicos
    c.execute('''CREATE TABLE IF NOT EXISTS invite_links
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  chat_id INTEGER,
                  link TEXT,
                  created_date DATETIME,
                  used BOOLEAN DEFAULT FALSE,
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    conn.commit()
    conn.close()

init_db()

# Funções de banco de dados
def get_db_connection():
    return sqlite3.connect('subscriptions.db')

def add_user(user_id: int, username: str, full_name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, full_name, date_joined) VALUES (?, ?, ?, ?)",
              (user_id, username, full_name, datetime.now()))
    conn.commit()
    conn.close()

def get_user_subscription(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM subscriptions WHERE user_id = ? AND active = TRUE ORDER BY expiration_date DESC LIMIT 1",
              (user_id,))
    subscription = c.fetchone()
    conn.close()
    return subscription

def add_subscription(user_id: int, plan: str, payment_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    start_date = datetime.now()
    expiration_date = start_date + timedelta(days=30)
    c.execute("INSERT INTO subscriptions (user_id, plan, start_date, expiration_date, active, payment_id) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, plan, start_date, expiration_date, True, payment_id))
    conn.commit()
    conn.close()
    return expiration_date

def update_subscription_status(payment_id: str, status: bool):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE subscriptions SET active = ? WHERE payment_id = ?",
              (status, payment_id))
    conn.commit()
    conn.close()

def get_expiring_subscriptions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, plan, expiration_date FROM subscriptions WHERE active = TRUE AND expiration_date <= ?",
              (datetime.now() + timedelta(days=1),))
    subscriptions = c.fetchall()
    conn.close()
    return subscriptions

def get_expired_subscriptions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, plan FROM subscriptions WHERE active = TRUE AND expiration_date < ?",
              (datetime.now(),))
    subscriptions = c.fetchall()
    conn.close()
    return subscriptions

# Funções da API Mercado Pago
async def create_mercadopago_payment(user_id: int, plan: str, amount: float):
    """Cria um pagamento real no Mercado Pago"""
    try:
        # Dados do item
        item_title = f"Plano {plan.upper()}" 
        item_quantity = 1
        unit_price = amount
        
        # Criar preferência de pagamento
        preference_data = {
            "items": [
                {
                    "title": item_title,
                    "quantity": item_quantity,
                    "currency_id": "BRL",
                    "unit_price": unit_price
                }
            ],
            "back_urls": {
                "success": f"{WEBHOOK_URL}/success",
                "failure": f"{WEBHOOK_URL}/failure", 
                "pending": f"{WEBHOOK_URL}/pending"
            },
            "auto_return": "approved",
            "notification_url": f"{WEBHOOK_URL}/webhook",
            "external_reference": f"user_{user_id}_plan_{plan}"
        }
        
        # Criar preferência
        preference_result = sdk.preference().create(preference_data)
        
        if preference_result["status"] == 201:
            preference = preference_result["response"]
            return {
                "success": True,
                "preference_id": preference["id"],
                "payment_url": preference["init_point"],
                "sandbox_init_point": preference["sandbox_init_point"]
            }
        else:
            return {"success": False, "error": "Erro ao criar pagamento"}
            
    except Exception as e:
        logger.error(f"Erro ao criar pagamento Mercado Pago: {e}")
        return {"success": False, "error": str(e)}

async def check_mercadopago_payment(payment_id: str):
    """Verifica o status de um pagamento no Mercado Pago"""
    try:
        payment_result = sdk.payment().get(payment_id)
        
        if payment_result["status"] == 200:
            payment = payment_result["response"]
            return {
                "success": True,
                "status": payment["status"],
                "approved": payment["status"] == "approved",
                "external_reference": payment.get("external_reference", "")
            }
        else:
            return {"success": False, "error": "Pagamento não encontrido"}
            
    except Exception as e:
        logger.error(f"Erro ao verificar pagamento: {e}")
        return {"success": False, "error": str(e)}

# Handlers de comandos
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.full_name)
    
    welcome_text = (
        "👋 Olá! Bem-vindo ao sistema de assinaturas!\n\n"
        "Comandos disponíveis:\n"
        "/assinar - Assinar um plano\n" 
        "/planos - Ver informações dos planos\n"
        "/suporte - Entrar em contato conosco\n\n"
        "Pagamentos processados via Mercado Pago."
    )
    
    await update.message.reply_text(welcome_text)

async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("😋 PLANO CONTOS VIP 😋", callback_data="plano_contos")],
        [InlineKeyboardButton("🤤 PLANO ECVP+ 🤤", callback_data="plano_ecvp")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Escolha um plano para ver mais detalhes:",
        reply_markup=reply_markup
    )

async def plano_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "plano_contos":
        text = (
            "😋 PLANO CONTOS VIP 😋\n\n"
            "O Plano Contos permite o acesso apenas ao CANAL DE CONTOS e GRUPO DE INFORMAÇÕES DO VIP.\n\n"
            "Você tem acesso a:\n"
            "- Acervo de Contos vip\n"
            "- Grupo Geral de Informações.\n\n"
            "Valor: R$ 35,00\n\n"
            "Para pagamentos internacionais, entre em contato com o suporte."
        )
        keyboard = [[InlineKeyboardButton("💰 Assinar Agora - R$ 35", callback_data="assinar_contos")]]
        
    elif query.data == "plano_ecvp":
        text = (
            "🤤 PLANO ECVP+ 🤤\n\n"
            "O PLANO ECVP+ fornece uma experiência completa com acesso a todos os conteúdos.\n\n"
            "Você terá acesso a:\n"
            "- Canal de Contos vip\n"
            "- Grupo de Midias+ vip\n"
            "- Grupo Geral de Informações\n\n"
            "Valor: R$ 55,00\n\n"
            "Para pagamentos internacionais, entre em contato conosco via /suporte."
        )
        keyboard = [[InlineKeyboardButton("💰 Assinar Agora - R$ 55", callback_data="assinar_ecvp")]]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)

async def assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("😋 Plano Contos - R$ 35", callback_data="assinar_contos")],
        [InlineKeyboardButton("🤤 Plano ECVP - R$ 55", callback_data="assinar_ecvp")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Escolha o plano que deseja assinar:",
        reply_markup=reply_markup
    )

async def process_assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    plan = "contos" if query.data == "assinar_contos" else "ecvp"
    amount = 35.0 if plan == "contos" else 55.0
    
    # Criar pagamento no Mercado Pago
    payment_result = await create_mercadopago_payment(user_id, plan, amount)
    
    if payment_result["success"]:
        payment_url = payment_result.get("sandbox_init_point") or payment_result["payment_url"]
        
        text = (
            f"💳 Pagamento para o Plano {plan.upper()}\n\n"
            f"Valor: R$ {amount:.2f}\n\n"
            f"🔗 Link para pagamento:\n{payment_url}\n\n"
            f"📋 Após o pagamento, sua assinatura será ativada automaticamente.\n\n"
            f"ℹ️ ID da preferência: {payment_result['preference_id']}\n\n"
            f"💬 Para pagamentos internacionais, use /suporte"
        )
        
    else:
        text = (
            f"❌ Erro ao criar pagamento:\n{payment_result.get('error', 'Erro desconhecido')}\n\n"
            f"Por favor, tente novamente ou entre em contato conosco via /suporte."
        )
    
    await query.edit_message_text(text)

async def suporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    
    # Encaminhar mensagem para os administradores
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📨 Novo ticket de suporte de {user.full_name} (@{user.username})\n\n"
                f"Mensagem: {message.text.replace('/suporte', '').strip()}"
            )
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para admin {admin_id}: {e}")
    
    await message.reply_text(
        "✅ Seu ticket de suporte foi enviado! Entraremos em contato em breve."
    )

async def advip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Verificar se é admin
    if user.id not in ADMINS:
        # Comando invisível para usuários normais - não responde nada
        return
    
    # Pedir ID do usuário (apenas para admins)
    await update.message.reply_text(
        "👤 Para adicionar um usuário manualmente ao VIP, envie o ID do usuário.\n\n"
        "Você pode obter o ID com @userinfobot ou outros bots de informação."
    )
    
    # Definir estado para esperar o ID do usuário
    context.user_data['awaiting_user_id'] = True

async def handle_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Verificar se é admin e está esperando um ID
    if user.id in ADMINS and context.user_data.get('awaiting_user_id'):
        try:
            user_id = int(update.message.text)
            
            # Verificar se o usuário já é VIP
            existing_sub = get_user_subscription(user_id)
            
            if existing_sub:
                await update.message.reply_text("❌ Este usuário já possui uma assinatura ativa.")
                context.user_data['awaiting_user_id'] = False
                return
            
            # Mostrar opções de plano
            keyboard = [
                [InlineKeyboardButton("Plano Contos", callback_data=f"admin_add_{user_id}_contos")],
                [InlineKeyboardButton("Plano ECVP", callback_data=f"admin_add_{user_id}_ecvp")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Selecione o plano para este usuário:",
                reply_markup=reply_markup
            )
            
            context.user_data['awaiting_user_id'] = False
            
        except ValueError:
            await update.message.reply_text("❌ ID inválido. Por favor, envie um ID numérico válido.")

async def admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    
    # Verificar se é admin
    if user.id not in ADMINS:
        await query.edit_message_text("❌ Você não tem permissão para usar este comando.")
        return
    
    # Extrair dados do callback_data: admin_add_{user_id}_{plan}
    parts = query.data.split('_')
    user_id = int(parts[2])
    plan = parts[3]
    
    # Adicionar assinatura manualmente
    expiration_date = add_subscription(user_id, plan, f"admin_added_{datetime.now().timestamp()}")
    
    await query.edit_message_text(f"✅ Usuário {user_id} adicionado ao plano {plan} com sucesso!")

# Tarefas em segundo plano
async def check_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Remove usuários com assinaturas expiradas"""
    expired_subs = get_expired_subscriptions()
    
    for user_id, plan in expired_subs:
        try:
            # Aqui você implementaria a remoção dos grupos
            # await remove_user_from_chats(user_id)
            
            # Atualizar banco
            update_subscription_status(None, False)
            
            # Notificar usuário
            await context.bot.send_message(
                user_id,
                "❌ Sua assinatura expirou. Para continuar tendo acesso, renove sua assinatura usando /assinar"
            )
        except Exception as e:
            logger.error(f"Erro ao processar assinatura expirada do usuário {user_id}: {e}")

async def notify_expiring_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Notifica usuários com assinaturas prestes a expirar"""
    expiring_subs = get_expiring_subscriptions()
    
    for user_id, plan, expiration_date in expiring_subs:
        try:
            exp_time = datetime.strptime(expiration_date, "%Y-%m-%d %H:%M:%S.%f") if isinstance(expiration_date, str) else expiration_date
            exp_str = exp_time.strftime("%d/%m/%Y às %H:%M")
            
            await context.bot.send_message(
                user_id,
                f"⚠️ Sua assinatura expira em {exp_str}. Caso não seja renovada, você perderá o acesso aos grupos/canais."
            )
        except Exception as e:
            logger.error(f"Erro ao notificar usuário {user_id}: {e}")

def main():
    # Criar aplicação com job queue
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers de comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("planos", planos))
    application.add_handler(CommandHandler("assinar", assinar))
    application.add_handler(CommandHandler("suporte", suporte))
    
    # Comando advip apenas para admins - não aparece na lista de comandos
    application.add_handler(CommandHandler("advip", advip))
    
    # Handlers de callback (botões)
    application.add_handler(CallbackQueryHandler(plano_details, pattern="^plano_"))
    application.add_handler(CallbackQueryHandler(process_assinar, pattern="^assinar_"))
    application.add_handler(CallbackQueryHandler(admin_add_user, pattern="^admin_add_"))
    
    # Handler para mensagens de texto (para o comando advip)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_id))
    
    # Handlers para palavras-chave de saudação
    application.add_handler(MessageHandler(filters.Regex(r"(?i)(olá|oi|opa|hola|eai|start|\.start)"), start))
    
    # Jobs periódicos
    job_queue = application.job_queue
    
    # Verificar assinaturas expiradas a cada hora
    job_queue.run_repeating(check_expired_subscriptions, interval=3600, first=10)
    
    # Verificar assinaturas que expiram hoje uma vez por dia
    job_queue.run_repeating(notify_expiring_subscriptions, interval=86400, first=60)
    
    # Iniciar bot
    application.run_polling()

if __name__ == '__main__':
    main()