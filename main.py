# -*- coding: utf-8 -*-
import logging
import os
from datetime import datetime, timedelta, time
import pytz
from functools import wraps

import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, error as telegram_error)
from telegram.ext import (Application, CommandHandler, ConversationHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler)

# --- Configurações ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variáveis de Ambiente e Constantes ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ADMIN_IDS_STR = os.environ.get('ADMIN_IDS', '')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]
SAO_PAULO_TZ = pytz.timezone("America/Sao_Paulo")

# --- Conexão com Firebase ---
try:
    cred = credentials.Certificate("credentials.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Conexão com Firebase (Firestore) estabelecida.")
except Exception as e:
    logger.error(f"CRÍTICO: Falha ao conectar ao Firebase: {e}")
    db = None

# --- Estados da Conversa ---
(AWAITING_CHANNEL, AWAITING_MEDIA, AWAITING_TEXT, AWAITING_BUTTON_PROMPT, 
 AWAITING_BUTTON_TEXT, AWAITING_BUTTON_URL, AWAITING_PIN_OPTION, AWAITING_SCHEDULE_TIME,
 AWAITING_INTERVAL, AWAITING_REPETITIONS, AWAITING_START_TIME, AWAITING_CONFIRMATION) = range(12)

# --- Decorator de Restrição ---
def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.callback_query: await update.callback_query.answer("Acesso Negado!", show_alert=True)
            else: await update.message.reply_text("🔒 Acesso Negado!")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Funções do Agendador (Scheduler) ---
async def send_post(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    schedule_id = job.data["schedule_id"]
    doc_ref = db.collection('schedules').document(schedule_id)
    post_doc = doc_ref.get()

    if not post_doc.exists:
        logger.warning(f"Post {schedule_id} não encontrado. Removendo job.")
        job.schedule_next_run_time = None
        return
    
    post = post_doc.to_dict()
    chat_id = post["chat_id"]
    text = post.get("text", "")
    media_file_id = post.get("media_file_id")
    media_type = post.get("media_type")
    buttons_data = post.get("buttons", [])
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(b['text'], url=b['url'])] for b in buttons_data]) if buttons_data else None

    try:
        sent_message = None
        if media_type == "photo":
            sent_message = await context.bot.send_photo(chat_id=chat_id, photo=media_file_id, caption=text, reply_markup=reply_markup, parse_mode='Markdown')
        elif media_type == "video":
            sent_message = await context.bot.send_video(chat_id=chat_id, video=media_file_id, caption=text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            sent_message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown')
        
        logger.info(f"Post {schedule_id} enviado para o chat {chat_id}.")
  
        if post.get("pin_post") and sent_message:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_message.message_id, disable_notification=True)

        if post["type"] == "agendada":
            doc_ref.delete()
        elif post.get("repetitions") is not None:
            if post["repetitions"] == 1: doc_ref.delete()
            elif post["repetitions"] != 0: doc_ref.update({"repetitions": firestore.Increment(-1)})
            
    except Exception as e:
        logger.error(f"Falha ao enviar post {schedule_id}: {e}")

async def reload_jobs_from_db(application: Application):
    if db is None: return
    logger.info("--- Recarregando jobs do Firestore ---")
    current_time = datetime.now(SAO_PAULO_TZ)
    jobs_reloaded, jobs_deleted = 0, 0
    
    for post_doc in db.collection('schedules').stream():
        post = post_doc.to_dict()
        schedule_id_str = post_doc.id

        if post['type'] == 'agendada':
            run_date = post.get('scheduled_for')
            if run_date and run_date > current_time:
                application.job_queue.run_once(send_post, run_date, name=schedule_id_str, data={"schedule_id": schedule_id_str})
                jobs_reloaded += 1
            elif run_date:
                post_doc.reference.delete()
                jobs_deleted += 1
        elif post['type'] == 'recorrente':
            start_date = post.get('start_date')
            if start_date and (post.get('repetitions', 1) > 0 or post.get('repetitions') == 0):
                interval_str = post['interval']
                unit = interval_str[-1]; value = int(interval_str[:-1])
                interval_kwargs = {'minutes': value} if unit == 'm' else {'hours': value} if unit == 'h' else {'days': value}
                
                application.job_queue.run_repeating(send_post, interval=timedelta(**interval_kwargs), first=start_date, name=schedule_id_str, data={"schedule_id": schedule_id_str})
                jobs_reloaded += 1
                
    logger.info(f"--- Recarregamento finalizado. {jobs_reloaded} reativados, {jobs_deleted} removidos. ---")

# --- Lógica do ConversationHandler ---
@restricted
async def start_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    schedule_type = 'agendada' if 'single' in query.data else 'recorrente'
    context.user_data.clear()
    context.user_data['type'] = schedule_type
    await query.edit_message_text("Ok, vamos criar um agendamento.\n\nPrimeiro, envie o ID ou @username do canal de destino.")
    return AWAITING_CHANNEL

@restricted
async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['chat_id'] = update.message.text
    await update.message.reply_text("Canal salvo.\n\nAgora envie a foto ou vídeo. Se for apenas texto, digite /pular.")
    return AWAITING_MEDIA

@restricted
async def get_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if message.photo:
        context.user_data['media_file_id'] = message.photo[-1].file_id
        context.user_data['media_type'] = 'photo'
    elif message.video:
        context.user_data['media_file_id'] = message.video.file_id
        context.user_data['media_type'] = 'video'
    await update.message.reply_text("Mídia salva.\n\nAgora, digite o texto da postagem. Use formatação *Markdown* se desejar.", parse_mode='Markdown')
    return AWAITING_TEXT

@restricted
async def skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['media_file_id'] = None
    context.user_data['media_type'] = None
    await update.message.reply_text("Ok, sem mídia.\n\nAgora, digite o texto da postagem. Use formatação *Markdown* se desejar.", parse_mode='Markdown')
    return AWAITING_TEXT

@restricted
async def get_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['text'] = update.message.text
    reply_keyboard = [["Sim"], ["Não"]]
    await update.message.reply_text(
        "Texto salvo.\n\nDeseja adicionar um botão de URL?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return AWAITING_BUTTON_PROMPT

@restricted
async def get_button_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.lower() == 'sim':
        await update.message.reply_text("Ok, envie o texto para o botão.", reply_markup=ReplyKeyboardRemove())
        return AWAITING_BUTTON_TEXT
    else:
        await update.message.reply_text("Ok, sem botões.\n\nDeseja fixar esta mensagem no canal?", reply_markup=ReplyKeyboardMarkup([["Sim"], ["Não"]], one_time_keyboard=True, resize_keyboard=True))
        return AWAITING_PIN_OPTION

@restricted
async def get_button_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault('buttons', []).append({'text': update.message.text})
    await update.message.reply_text("Texto do botão salvo.\n\nAgora envie a URL completa (ex: https://google.com).")
    return AWAITING_BUTTON_URL

@restricted
async def get_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['buttons'][-1]['url'] = update.message.text
    await update.message.reply_text("Botão salvo.\n\nDeseja fixar a postagem no canal?", reply_markup=ReplyKeyboardMarkup([["Sim"], ["Não"]], one_time_keyboard=True, resize_keyboard=True))
    return AWAITING_PIN_OPTION

@restricted
async def get_pin_option(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['pin_post'] = (update.message.text.lower() == 'sim')
    await update.message.reply_text("Entendido.", reply_markup=ReplyKeyboardRemove())
    
    if context.user_data['type'] == 'agendada':
        await update.message.reply_text("Agora envie a data e hora do agendamento no formato: DD/MM/AAAA HH:MM")
        return AWAITING_SCHEDULE_TIME
    else:
        await update.message.reply_text("Agora defina o intervalo. Ex: 30m, 12h, 1d (minutos, horas, dias).")
        return AWAITING_INTERVAL

@restricted
async def get_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dt_obj = datetime.strptime(update.message.text, '%d/%m/%Y %H:%M')
        context.user_data['scheduled_for'] = SAO_PAULO_TZ.localize(dt_obj)
        await confirm_schedule(update, context)
        return AWAITING_CONFIRMATION
    except ValueError:
        await update.message.reply_text("Formato inválido. Tente novamente: DD/MM/AAAA HH:MM")
        return AWAITING_SCHEDULE_TIME

@restricted
async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['interval'] = update.message.text.lower()
    await update.message.reply_text("Intervalo salvo.\n\nQuantas vezes deve repetir? (Digite 0 para infinito)")
    return AWAITING_REPETITIONS

@restricted
async def get_repetitions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['repetitions'] = int(update.message.text)
    await update.message.reply_text("Repetições salvas.\n\nQual a data e hora de início? (DD/MM/AAAA HH:MM)")
    return AWAITING_START_TIME

@restricted
async def get_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dt_obj = datetime.strptime(update.message.text, '%d/%m/%Y %H:%M')
        context.user_data['start_date'] = SAO_PAULO_TZ.localize(dt_obj)
        await confirm_schedule(update, context)
        return AWAITING_CONFIRMATION
    except ValueError:
        await update.message.reply_text("Formato inválido. Tente novamente: DD/MM/AAAA HH:MM")
        return AWAITING_START_TIME

async def confirm_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    summary = "📋 *Resumo do Agendamento*\n\n"
    summary += f"▪️ **Tipo:** `{data['type'].capitalize()}`\n"
    summary += f"▪️ **Destino:** `{data['chat_id']}`\n"
    if data.get('media_type'):
        summary += f"▪️ **Mídia:** `{data['media_type'].capitalize()}`\n"
    summary += f"▪️ **Fixar:** `{'Sim' if data.get('pin_post') else 'Não'}`\n"
    if data.get('buttons'):
        summary += f"▪️ **Botões:** `{len(data['buttons'])}`\n"
    
    if data['type'] == 'agendada':
        dt = data.get('scheduled_for').strftime('%d/%m/%Y às %H:%M')
        summary += f"\n🗓️ **Agendado para:** {dt}"
    else:
        dt = data.get('start_date').strftime('%d/%m/%Y às %H:%M')
        rep = "Infinitas" if data.get('repetitions') == 0 else data.get('repetitions')
        summary += f"\n▶️ **Início em:** {dt}\n"
        summary += f"⏳ **Intervalo:** A cada `{data.get('interval')}`\n"
        summary += f"🔁 **Repetições:** `{rep}`"

    await update.message.reply_text(summary, parse_mode='Markdown')
    await update.message.reply_text(
        "Confirma o agendamento?",
        reply_markup=ReplyKeyboardMarkup([["✅ Confirmar"], ["❌ Cancelar"]], one_time_keyboard=True, resize_keyboard=True)
    )

@restricted
async def save_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_data = context.user_data
        user_data['created_at'] = firestore.SERVER_TIMESTAMP
        user_data['user_id'] = update.effective_user.id

        _ , doc_ref = db.collection('schedules').add(user_data)
        schedule_id = doc_ref.id
        post_data = {"schedule_id": schedule_id}
        
        if user_data['type'] == 'agendada':
            context.application.job_queue.run_once(send_post, user_data['scheduled_for'], data=post_data, name=schedule_id)
        else:
            interval_str = user_data['interval']
            unit = interval_str[-1]; value = int(interval_str[:-1])
            interval_kwargs = {'minutes': value} if unit == 'm' else {'hours': value} if unit == 'h' else {'days': value}
            context.application.job_queue.run_repeating(send_post, interval=timedelta(**interval_kwargs), first=user_data['start_date'], data=post_data, name=schedule_id)
        
        await update.message.reply_text("✅ Agendamento criado com sucesso!", reply_markup=ReplyKeyboardRemove())
        await show_main_menu(update, context, is_new_message=True)

    except Exception as e:
        logger.error(f"Erro ao salvar agendamento: {e}")
        await update.message.reply_text("❌ Ocorreu um erro ao salvar.", reply_markup=ReplyKeyboardRemove())
    
    context.user_data.clear()
    return ConversationHandler.END

@restricted
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada.", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END
    
# --- Funções de Menu ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("🆕 Agendar Postagem", callback_data='start_schedule_single')],
        [InlineKeyboardButton("🔁 Agendar Recorrente", callback_data='start_schedule_recurrent')],
        [InlineKeyboardButton("📋 Listar Agendamentos", callback_data='list_schedules')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = "👇 Escolha uma opção:"
    if update.callback_query and not is_new_message:
        try:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
        except telegram_error.BadRequest as e:
            if "Message is not modified" not in str(e): logger.warning(f"Erro ao editar menu: {e}")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=message_text, reply_markup=reply_markup)

@restricted
async def list_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if db is None:
        await query.message.reply_text("⚠️ Erro de conexão com o banco de dados.")
        return

    message = "📅 *Suas Postagens Agendadas*\n\n"
    found_any = False
    
    user_posts_query = db.collection('schedules').where('user_id', '==', update.effective_user.id).order_by('created_at', direction=firestore.Query.DESCENDING)
    
    for doc in user_posts_query.stream():
        found_any = True
        post = doc.to_dict()
        
        message += f"🆔 `{doc.id}`\n"
        #... (lógica de formatação da lista, igual à anterior)
        message += "\n"

    if not found_any: message = "Você ainda não tem postagens agendadas."
    
    await query.edit_message_text(message, parse_mode='Markdown')
    # Adicionar um botão para voltar ao menu principal
    await query.message.reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]]))

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_main_menu(update, context)

@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Olá, {update.effective_user.first_name}!", parse_mode='Markdown')
    await show_main_menu(update, context, is_new_message=True)

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ocorreu uma exceção: {context.error}", exc_info=context.error)

# --- Função Principal ---
def main() -> None:
    if not all([TELEGRAM_TOKEN, db, ADMIN_IDS]):
        logger.error("FATAL: Variáveis de ambiente ou conexão com DB ausentes.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_schedule_flow, pattern='^start_schedule_')],
        states={
            AWAITING_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)],
            AWAITING_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, get_media), CommandHandler('pular', skip_media)],
            AWAITING_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_text)],
            AWAITING_BUTTON_PROMPT: [MessageHandler(filters.Regex('^(Sim|Não)$'), get_button_prompt)],
            AWAITING_BUTTON_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_text)],
            AWAITING_BUTTON_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_button_url)],
            AWAITING_PIN_OPTION: [MessageHandler(filters.Regex('^(Sim|Não)$'), get_pin_option)],
            AWAITING_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_time)],
            AWAITING_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_interval)],
            AWAITING_REPETITIONS: [MessageHandler(filters.Regex(r'^\d+$'), get_repetitions)],
            AWAITING_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start_time)],
            AWAITING_CONFIRMATION: [MessageHandler(filters.Regex('^✅ Confirmar$'), save_schedule)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex('^❌ Cancelar$'), cancel)],
        per_message=False
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(list_schedules, pattern='list_schedules'))
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='back_to_main_menu'))
    
    application.post_init = reload_jobs_from_db
    
    logger.info("🚀 Bot em execução...")
    application.run_polling()

if __name__ == '__main__':
    main()
