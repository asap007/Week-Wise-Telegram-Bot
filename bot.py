import os
import csv
import logging
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile, Message
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from telegram.error import TelegramError, Unauthorized, BadRequest, TimedOut, NetworkError

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID'))  # Single main admin
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').split(',') if x]  # Initial list of sub-admins
USER_EMAIL = os.getenv('USER_EMAIL')

user_message_ids = {}

# Google Sheets setup
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']
SERVICE_ACCOUNT_FILE = 'credentials.json'
credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPE)
service = build('sheets', 'v4', credentials=credentials)

current_spreadsheet_id = None
week_count = 1
last_sheet_creation_date = None
questions = [
    "1) Brief summary of your week:",
    "2) New projects you are working on:",
    "3) Points of attention for the team:",
    "4) Any other activities you want to mention:"
]
responses = {}

def create_new_sheet():
    global current_spreadsheet_id, week_count, last_sheet_creation_date
    try:
        header = ['User ID', 'Name', 'Username', 'Date'] + questions
        spreadsheet = {
            'properties': {'title': f'Week {week_count} Responses'},
            'sheets': [{
                'data': [{
                    'startRow': 0,
                    'startColumn': 0,
                    'rowData': [{'values': [{'userEnteredValue': {'stringValue': h}} for h in header]}]
                }]
            }]
        }
        spreadsheet = service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
        current_spreadsheet_id = spreadsheet.get('spreadsheetId')
        
        drive_service = build('drive', 'v3', credentials=credentials)
        
        # Share with service account
        drive_service.permissions().create(
            fileId=current_spreadsheet_id,
            body={'type': 'user', 'role': 'writer', 'emailAddress': 'sheets@youtube-435902.iam.gserviceaccount.com'}
        ).execute()
        
        # Share with user's personal email
        drive_service.permissions().create(
            fileId=current_spreadsheet_id,
            body={'type': 'user', 'role': 'writer', 'emailAddress': USER_EMAIL}
        ).execute()
        
        week_count += 1
        last_sheet_creation_date = datetime.now()
        return current_spreadsheet_id
    except Exception as e:
        logger.error(f"Error creating new sheet: {e}")
        return None

def start(update: Update, context: CallbackContext):
    try:
        keyboard = [[InlineKeyboardButton("Gathering Weekly Updates", callback_data='start_form')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.message:
            update.message.reply_text("Hi\n\nRegister your weekly activity overview by clicking the button below. \n\nCarefully read each question before you answer to make the process easier for everyone.", reply_markup=reply_markup)
        else:
            context.bot.send_message(chat_id=update.callback_query.from_user.id,
                                     text="Hi\n\nRegister your weekly activity overview by clicking the button below. \n\nCarefully read each question before you answer to make the process easier for everyone.",
                                     reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        if update.message:
            update.message.reply_text("An error occurred. Please try again later.")
        else:
            context.bot.send_message(chat_id=update.callback_query.from_user.id,
                                     text="An error occurred. Please try again later.")

def edit_questions(update: Update, context: CallbackContext):
    if update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("Only the main admin can edit questions.")
        return

    try:
        if context.args:
            command = context.args[0].lower()
            if command == "add":
                new_question = " ".join(context.args[1:])
                questions.append(new_question)
                update.message.reply_text(f"New question added: {new_question}")
            elif command == "remove":
                index = int(context.args[1]) - 1
                if 0 <= index < len(questions):
                    removed_question = questions.pop(index)
                    update.message.reply_text(f"Question removed: {removed_question}")
                else:
                    update.message.reply_text("Invalid question number.")
            else:
                update.message.reply_text("Invalid command. Use `/editquestions add <question>` or `/editquestions remove <number>`.")
        else:
            update.message.reply_text("Current questions:\n" + "\n".join([f"{i+1}) {q}" for i, q in enumerate(questions)]))
    except Exception as e:
        logger.error(f"Error in edit_questions command: {e}")
        update.message.reply_text("An error occurred while editing questions. Please try again.")

def button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if query.data == 'start_form':
        responses[user_id] = []
        # Delete the `/start` message
        if query.message:
            context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        # Clear previous message ID
        context.user_data['prev_message_id'] = None
        send_question(chat_id, 0, context)
    elif query.data == 'back_to_start':
        if user_id in responses:
            del responses[user_id]
        # Delete the current message (Back to Start button)
        context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        # Clear previous message ID
        context.user_data['prev_message_id'] = None
        start(update, context)
    elif query.data == 'back_to_main_menu':
        # Delete the form completion message
        context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        # Clear previous message ID
        context.user_data['prev_message_id'] = None
        start(update, context)
    elif query.data.startswith('back_to_question_'):
        question_index = int(query.data.split('_')[-1])
        responses[user_id] = responses[user_id][:question_index]
        # Delete the current question message
        context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        # Send the previous question
        send_question(chat_id, question_index, context)

def send_question(chat_id, question_index, context):
    keyboard = []
    if question_index > 0:
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f'back_to_question_{question_index-1}')])
    else:
        keyboard.append([InlineKeyboardButton("⬅️ Back to Start", callback_data='back_to_start')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    question_text = questions[question_index]
    
    try:
        # Delete the previous question message to avoid duplication
        prev_message_id = context.user_data.get('prev_message_id')
        if prev_message_id:
            try:
                context.bot.delete_message(chat_id=chat_id, message_id=prev_message_id)
            except Exception as e:
                logger.warning(f"Could not delete message {prev_message_id}: {e}")
        # Send the new question
        new_message = context.bot.send_message(chat_id=chat_id, text=question_text, reply_markup=reply_markup)
        # Save the new message's ID
        context.user_data['prev_message_id'] = new_message.message_id
    except Exception as e:
        logger.error(f"Error sending question: {e}")
        context.bot.send_message(chat_id=chat_id, text="An error occurred. Please try again.")


def receive_response(update: Update, context: CallbackContext):
    user = update.message.from_user
    user_id = user.id
    current_response = update.message.text

    if user_id in responses:
        responses[user_id].append(current_response)

        if len(responses[user_id]) < len(questions):
            # Delete the user's message containing their response
            context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
            # Send the next question
            send_question(update.message.chat_id, len(responses[user_id]), context)
        else:
            try:
                save_response_to_sheet(user, responses[user_id])
                del responses[user_id]

                # Delete the user's message containing their last response
                context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
                # Delete the previous question message
                prev_message_id = context.user_data.get('prev_message_id')
                if prev_message_id:
                    context.bot.delete_message(chat_id=update.message.chat_id, message_id=prev_message_id)
                    context.user_data['prev_message_id'] = None

                # Display final message
                keyboard = [[InlineKeyboardButton("Back to Main Menu", callback_data='back_to_start')]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                final_msg = context.bot.send_message(chat_id=update.message.chat_id, text="✅ Form completed!", reply_markup=reply_markup)
                # Store the final message ID in case we need to delete it
                context.user_data['prev_message_id'] = final_msg.message_id
            except Exception as e:
                logger.error(f"Error saving response: {e}")
                context.bot.send_message(chat_id=update.message.chat_id, text="An error occurred while saving your response. Please try again later.")
    else:
        context.bot.send_message(chat_id=update.message.chat_id, text="Please start the form by clicking the button.")

def save_response_to_sheet(user, user_responses):
    global current_spreadsheet_id, last_sheet_creation_date

    if not current_spreadsheet_id or (datetime.now() - last_sheet_creation_date).days >= 7:
        current_spreadsheet_id = create_new_sheet()

    if not current_spreadsheet_id:
        raise Exception("No active spreadsheet")

    sheet = service.spreadsheets()
    row = [
        str(user.id),
        user.full_name,
        user.username if user.username else "N/A",
        str(datetime.now())
    ] + user_responses

    body = {'values': [row]}
    sheet.values().append(
        spreadsheetId=current_spreadsheet_id,
        range='Sheet1!A1',
        valueInputOption='RAW',
        body=body
    ).execute()

def new_week(update: Update, context: CallbackContext):
    if update.message.from_user.id not in ADMIN_IDS and update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("You are not authorized to perform this action.")
        return

    try:
        new_sheet_id = create_new_sheet()
        if new_sheet_id:
            update.message.reply_text(f"New week started! Responses will be saved to sheet: {new_sheet_id}")
        else:
            update.message.reply_text("Failed to create a new sheet. Please try again later.")
    except Exception as e:
        logger.error(f"Error in new_week command: {e}")
        update.message.reply_text("An error occurred while creating a new week. Please try again later.")

def export_as_csv(update: Update, context: CallbackContext):
    if update.message.from_user.id not in ADMIN_IDS and update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("You are not authorized to perform this action.")
        return

    try:
        result = service.spreadsheets().values().get(spreadsheetId=current_spreadsheet_id, range='Sheet1').execute()
        rows = result.get('values', [])

        file_name = f'week_{week_count-1}_responses.csv'
        with open(file_name, 'w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerows(rows)

        with open(file_name, 'rb') as file:
            update.message.reply_document(InputFile(file), caption="Here is the CSV export for this week.")
        
        os.remove(file_name)  # Clean up the file after sending
    except Exception as e:
        logger.error(f"Error in export_as_csv command: {e}")
        update.message.reply_text("An error occurred while exporting the CSV. Please try again later.")

def list_weeks(update: Update, context: CallbackContext):
    if update.message.from_user.id not in ADMIN_IDS and update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("You are not authorized to perform this action.")
        return

    try:
        sheet_list = f"Weeks' Google Sheets:\n"
        for i in range(1, week_count):
            sheet_list += f"Week {i}: https://docs.google.com/spreadsheets/d/{current_spreadsheet_id}\n"
        update.message.reply_text(sheet_list)
    except Exception as e:
        logger.error(f"Error in list_weeks command: {e}")
        update.message.reply_text("An error occurred while listing weeks. Please try again later.")

def add_admin(update: Update, context: CallbackContext):
    if update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("Only the main admin can add sub-admins.")
        return

    try:
        new_admin_id = int(context.args[0])
        if new_admin_id not in ADMIN_IDS:
            ADMIN_IDS.append(new_admin_id)
            update.message.reply_text(f"User {new_admin_id} has been added as a sub-admin.")
        else:
            update.message.reply_text(f"User {new_admin_id} is already a sub-admin.")
    except (IndexError, ValueError):
        update.message.reply_text("Please provide a valid user ID to add as a sub-admin.")
    except Exception as e:
        logger.error(f"Error in add_admin command: {e}")
        update.message.reply_text("An error occurred while adding an admin. Please try again later.")

def remove_admin(update: Update, context: CallbackContext):
    if update.message.from_user.id != MAIN_ADMIN_ID:
        update.message.reply_text("Only the main admin can remove sub-admins.")
        return

    try:
        admin_id = int(context.args[0])
        if admin_id in ADMIN_IDS:
            ADMIN_IDS.remove(admin_id)
            update.message.reply_text(f"User {admin_id} has been removed as a sub-admin.")
        else:
            update.message.reply_text(f"User {admin_id} is not a sub-admin.")
    except (IndexError, ValueError):
        update.message.reply_text("Please provide a valid user ID to remove as a sub-admin.")
    except Exception as e:
        logger.error(f"Error in remove_admin command: {e}")
        update.message.reply_text("An error occurred while removing an admin. Please try again later.")

def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Update {update} caused error {context.error}")
    try:
        raise context.error
    except Unauthorized:
        # remove update.message.chat_id from conversation list
        logger.info(f"Unauthorized error for chat {update.effective_chat.id}")
    except BadRequest:
        # handle malformed requests
        logger.info(f"Bad Request for chat {update.effective_chat.id}")
    except TimedOut:
        # handle slow connection problems
        logger.info(f"Timed out for chat {update.effective_chat.id}")
    except NetworkError:
        # handle other connection problems
        logger.info(f"Network error for chat {update.effective_chat.id}")
    except TelegramError:
        # handle all other telegram related errors
        logger.info(f"Telegram error for chat {update.effective_chat.id}")

def main():
    global current_spreadsheet_id, last_sheet_creation_date
    current_spreadsheet_id = create_new_sheet()  # Initialize with a new sheet for the current week

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("newweek", new_week))
    dp.add_handler(CommandHandler("exportcsv", export_as_csv))
    dp.add_handler(CommandHandler("listweeks", list_weeks))
    dp.add_handler(CommandHandler("addadmin", add_admin))
    dp.add_handler(CommandHandler("removeadmin", remove_admin))
    dp.add_handler(CommandHandler("editquestions", edit_questions))
    dp.add_handler(CallbackQueryHandler(button))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, receive_response))

    # Add error handler
    dp.add_error_handler(error_handler)

    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == '__main__':
    main()