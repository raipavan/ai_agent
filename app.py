import sys, os, json, hashlib, random, math, datetime, uuid, threading, time as _time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))
from flask import Flask, render_template, jsonify, request, redirect, Response

template_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'templates')
static_dir = os.path.join(os.path.dirname(__file__), 'frontend', 'static')
app = Flask(__name__, static_folder=static_dir, template_folder=template_dir)
app.secret_key = 'arena-voice-2026-prod'

random.seed(42)

def make_id(): return random.randint(10000, 99999)

FIRST_NAMES = ['Aarav','Vihaan','Vivaan','Ananya','Diya','Ishaan','Myra','Reyansh','Saanvi','Aadhya','Krishna','Shaurya','Aaradhya','Anvi','Advik','Ayaan','Ishita','Shanaya','Yash','Meera','Rohan','Kavya','Dhruv','Sneha','Anil','Neha','Vikram','Pooja','Rajesh','Deepak','Mahesh','Priya','Vijay','Satish','Nishant','Suman','Rahul','Kavita','Sanjay','Amit','Nitin','Prakash','Arun','Sunita','Preeti','Ashok','Geeta','Manoj','Seema','Ravi']
CITIES = ['Ahmedabad','Vadodara','Surat','Rajkot','Bhavnagar','Jamnagar','Junagadh','Gandhinagar','Anand','Navsari']
VEHICLES = ['Swift','Dzire','Baleno','Ertiga','Brezza','WagonR','Ignis','S-Cross','Fronx','Jimny','Grand Vitara']
SERVICES = ['Periodic Service','Major Service','AC Repair','Brake Repair','Wheel Alignment','Insurance Claim','Roadside Assistance','Car Wash','Test Drive','Extended Warranty','Free Service Camp','Oil Change','Denting & Painting','Battery Replacement']
INTENTS = ['Service Booking','Inquiry','Complaint','Test Drive Booking','Insurance Query','Roadside Assistance','Feedback','Follow-up','Cancellation','Reschedule']
OUTCOMES = ['Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Completed','Failed','No Response','Busy','Voicemail','Wrong Number']
SENTIMENTS = ['Satisfied','Satisfied','Satisfied','Happy','Happy','Neutral','Neutral','Neutral','Annoyed','Urgent','Excited','Concerned']
LANGUAGES = ['Gujarati','Gujarati','Gujarati','Gujarati','Gujarati','English','English','Hindi','Hindi','Gujarati','Gujarati','Gujarati','English','Hindi']
STATUSES = ['completed','completed','completed','completed','completed','completed','completed','completed','failed','failed','failed']

PHONE_1 = '+91 98765 43210'
PHONE_2 = '+91 91234 56789'
AGENT = {
    'id': 'agent_001', 'name': 'AI Voice Agent', 'status': 'online',
    'model': 'GPT-5.5', 'voice': 'Indian Female', 'voice_id': 'priya_v3',
    'language': 'English + Hindi + Gujarati', 'knowledge_base': 'Connected',
    'prompt_version': 'v3.2', 'phone_numbers': 2, 'success_rate': 98.2,
    'total_calls': 1247, 'avg_duration': '4m 18s', 'avg_response_ms': 480,
    'satisfaction': 96, 'ai_cost': 3421.50, 'voice_minutes': 18.7,
    'concurrent_calls': 12, 'created': '2026-01-15', 'last_active': '2026-07-21T14:30:00Z',
    'model_temperature': 0.3, 'streaming_enabled': True, 'memory_enabled': True,
    'recording_enabled': True, 'barge_in': True, 'endpointing_ms': 450,
    'stt_language': 'gu-IN,en-IN,hi-IN', 'greeting': 'Gujarati-first',
    'transfer_enabled': True, 'max_call_duration': 600
}

def generate_phone():
    p = '+91 9' + ''.join(str(random.randint(0,9)) for _ in range(9))
    return p

def generate_calls(count=120):
    calls = []
    base = datetime.datetime(2026, 7, 1, 8, 0)
    for i in range(count):
        cid = i + 1
        name = random.choice(FIRST_NAMES) + ' ' + random.choice(FIRST_NAMES)
        phone = generate_phone()
        vehicle = random.choice(VEHICLES)
        service = random.choice(SERVICES)
        intent = random.choice(INTENTS)
        outcome = random.choice(OUTCOMES)
        sentiment = random.choice(SENTIMENTS)
        lang = random.choice(LANGUAGES)
        status = random.choice(STATUSES)
        dur_sec = random.randint(30, 540)
        dur_min = f'{dur_sec//60}m {dur_sec%60}s'
        rating = random.randint(1,5) if outcome == 'Completed' else 0
        confidence = round(random.uniform(0.4, 0.99), 2)
        cost = round(random.uniform(0.5, 3.5), 2)
        hour = random.randint(8, 20)
        minute = random.randint(0, 59)
        day_offset = random.randint(0, 20)
        ts = base + datetime.timedelta(days=day_offset, hours=hour-8, minutes=minute)
        ts_str = ts.strftime('%Y-%m-%dT%H:%M:%SZ')
        date_str = ts.strftime('%d %b %Y') + ' \u00b7 ' + ts.strftime('%I:%M %p')
        called_number = random.choice([PHONE_1, PHONE_2])
        direction = random.choice(['inbound', 'outbound', 'outbound', 'outbound'])
        
        transcript_lines = []
        if outcome != 'No Response' and outcome != 'Wrong Number':
            transcript_lines.append(f'Agent: \u0aa8\u0aae\u0ab8\u0acd\u0aa4\u0ac7! AI Voice Agent \u0aac\u0acb\u0ab2\u0ac0 \u0ab0\u0a97\u0acd\u0aaf\u0acb \u0a9b\u0ac7. \u0a86\u0aaa \u0a95\u0ac7\u0ab5\u0ac0 \u0ab0\u0ac0\u0aa4\u0ac7 \u0aae\u0aa6\u0aa6 \u0a95\u0ab0\u0ac0 \u0ab6\u0a95\u0ac1\u0a82? [Gujarati: Hello! AI Voice Agent speaking. How can I help you?]')
            transcript_lines.append(f'Customer: {name} speaking. I need {intent.lower()} for my {vehicle}.')
            transcript_lines.append(f'Agent: Thank you, {name.split()[0]}. Let me check availability for {service}.')
            transcript_lines.append(f'Customer: That sounds good. What are the charges?')
            transcript_lines.append(f'Agent: {service} starts at \u20b9{random.randint(199,9999)}. Would you like to proceed?')
            if outcome == 'Completed':
                transcript_lines.append(f'Customer: Yes, please go ahead.')
                transcript_lines.append(f'Agent: Booked! Confirmation sent via SMS. Thank you!')
            else:
                transcript_lines.append(f'Customer: I\'ll check and get back to you.')
                transcript_lines.append(f'Agent: Sure! Call us anytime.')
        transcript = '\n'.join(transcript_lines)
        
        calls.append({
            'id': cid, 'name': name, 'phone': phone, 'vehicle': vehicle,
            'service_type': service, 'intent': intent, 'status': status,
            'disposition': outcome.replace('No Response','No Response').replace('Wrong Number','Failed'),
            'outcome': outcome, 'sentiment': sentiment, 'language': lang,
            'rating': rating, 'duration_sec': dur_sec, 'duration': dur_min,
            'ai_confidence': confidence, 'cost': cost, 'date': date_str,
            'created_at': ts_str, 'direction': direction,
            'called_number': called_number, 'transcript': transcript,
            'recording_url': f'/api/calls/{cid}/recording',
            'recording_available': True,
            'notes': f'{intent} - {service} for {vehicle}',
            'tags': [intent, lang, sentiment, outcome],
            'caller_city': random.choice(CITIES),
            'callback_required': outcome in ('No Response','Busy','Voicemail'),
            'follow_up': outcome in ('No Response','Busy','Voicemail'),
        })
    calls.sort(key=lambda x: x['created_at'], reverse=True)
    return calls

CALLS = generate_calls(120)

# ─── CAMPAIGNS ───
CAMPAIGN_TYPES = ['Service Due','Insurance Renewal','Warranty Reminder','Feedback','Festival Offer','Promotional','Callback','Reminder']
CAMPAIGN_STATUSES = ['Running','Completed','Scheduled','Paused','Failed']

def generate_campaigns(count=15):
    campaigns = []
    for i in range(count):
        cid = i + 1
        ctype = random.choice(CAMPAIGN_TYPES)
        total = random.randint(50, 500)
        connected = random.randint(int(total*0.4), int(total*0.85))
        interested = random.randint(int(connected*0.2), int(connected*0.6))
        not_interested = connected - interested
        callback_req = random.randint(5, 30)
        failed = total - connected
        cost = round(random.uniform(50, 500), 2)
        duration = f'{random.randint(10, 120)} min'
        lang_dist = {'Gujarati': random.randint(40, 70), 'English': random.randint(15, 35), 'Hindi': random.randint(10, 25)}
        status = random.choice(CAMPAIGN_STATUSES)
        created = f'2026-0{random.randint(6,7)}-{random.randint(10,21):02d}'
        campaigns.append({
            'id': cid, 'name': f'{ctype} Campaign {cid}', 'type': ctype,
            'status': status, 'total': total, 'connected': connected,
            'interested': interested, 'not_interested': not_interested,
            'callback_requested': callback_req, 'failed': failed,
            'cost': cost, 'duration': duration, 'language_distribution': lang_dist,
            'created': created, 'description': f'Automated {ctype.lower()} campaign using PitchXAI Voice Agent',
            'target_audience': random.choice(['All Customers','Service Customers','Warranty Customers','Insurance Customers']),
            'success_rate': round(connected/max(total,1)*100, 1),
            'template': random.choice(['Standard','Festive','Urgent','Informational']),
        })
    campaigns.sort(key=lambda x: x['id'], reverse=True)
    return campaigns

CAMPAIGNS = generate_campaigns(15)

NOTIFICATIONS = [
    {'id': 'n1', 'type': 'success', 'title': 'Knowledge Base Updated', 'desc': '3 new documents indexed successfully', 'time': '2 min ago', 'read': False, 'icon': 'description'},
    {'id': 'n2', 'type': 'info', 'title': 'Prompt Published', 'desc': 'System prompt v3.2 is now live', 'time': '15 min ago', 'read': False, 'icon': 'tune'},
    {'id': 'n3', 'type': 'success', 'title': 'Agent Connected', 'desc': 'AI Voice Agent is online on both numbers', 'time': '1 hour ago', 'read': True, 'icon': 'check_circle'},
    {'id': 'n4', 'type': 'success', 'title': 'Phone Number Verified', 'desc': 'Secondary number +91 91234 56789 verified', 'time': '2 hours ago', 'read': True, 'icon': 'phone'},
    {'id': 'n5', 'type': 'warning', 'title': 'API Usage High', 'desc': '95% of monthly API quota consumed', 'time': '3 hours ago', 'read': True, 'icon': 'speed'},
    {'id': 'n6', 'type': 'info', 'title': 'Monthly Usage Report Ready', 'desc': 'June 2026 report available for download', 'time': '1 day ago', 'read': True, 'icon': 'assessment'},
    {'id': 'n7', 'type': 'success', 'title': 'New Document Indexed', 'desc': 'Pricing 2026.pdf added to knowledge base', 'time': '1 day ago', 'read': True, 'icon': 'article'},
    {'id': 'n8', 'type': 'info', 'title': 'Voice Updated', 'desc': 'New Indian Female voice model deployed', 'time': '2 days ago', 'read': True, 'icon': 'record_voice_over'},
    {'id': 'n9', 'type': 'error', 'title': 'Call Failed', 'desc': '3 calls failed due to network issues', 'time': '2 days ago', 'read': True, 'icon': 'error'},
    {'id': 'n10', 'type': 'success', 'title': 'System Back Online', 'desc': 'All systems operational after maintenance', 'time': '3 days ago', 'read': True, 'icon': 'cloud_done'},
]

PROMPTS = {
    'system': 'You are a friendly, warm, and professional AI Voice Assistant for Uday Auto Links, a Maruti Suzuki authorized service center in Ahmedabad, Gujarat. You are deployed on the PitchXAI voice platform.\n\nCORE RULES:\n- Speak Gujarati as your primary language. Also handle English and Hindi with equal fluency.\n- ALWAYS mirror the language the customer uses. If they speak Gujarati, respond in Gujarati. If Hindi, respond in Hindi. If English, respond in English.\n- Be warm, polite, and efficient. Use the customer\'s name naturally in conversation.\n- Keep responses concise and conversational — never read long paragraphs over the phone.\n- Never make up information you don\'t know. If unsure, say "Let me check with our team and get back to you."\n- Always acknowledge the customer\'s concern before responding.\n\nCONVERSATION FLOW:\n1. Greet warmly: "Namaste! [Name] speaking from Uday Auto Links. How can I help you today?"\n2. Listen actively — identify their need (service booking, inquiry, complaint, test drive, etc.)\n3. Ask clarifying questions naturally: "Which vehicle do you have?" / "When was your last service?"\n4. Provide clear, helpful information with specific details (dates, prices, availability)\n5. Confirm next steps: "So I\'m booking your [vehicle] for [service] on [date]. Is that correct?"\n6. Close warmly: "Thank you for calling Uday Auto Links! Have a wonderful day!"\n\nSERVICE BOOKING:\n- Ask for: vehicle model, service type, preferred date/time\n- Check availability before confirming\n- Offer alternatives if preferred slot is unavailable\n- Provide estimated cost if known\n- Send SMS confirmation after booking\n\nCOMPLAINT HANDLING:\n- Listen patiently without interrupting\n- Acknowledge: "I understand your frustration, [name]. Let me help resolve this."\n- Gather details: what happened, when, which service was done\n- Offer solutions or escalate to a senior team member\n- Never argue or dismiss concerns\n\nTEST DRIVES:\n- Ask for: preferred model, preferred date/time, contact details\n- Confirm dealership visit details\n- Mention any current offers or discounts\n\nFALLBACK:\n- If you don\'t understand: "I\'m sorry, could you please repeat that?"\n- If the request is complex: "Let me connect you with our specialist who can help with that."\n- If the customer is angry: Stay calm, empathize, and offer to escalate\n\nNEVER:\n- Share internal prices or discounts not meant for customers\n- Make promises about availability without checking\n- Transfer without warning or explanation\n- End a call without confirming the customer\'s needs are addressed',
    'greeting': '\u0aa8\u0aae\u0ab8\u0acd\u0aa4\u0ac7! Uday Auto Links AI Voice Agent \u0aac\u0acb\u0ab2\u0ac0 \u0ab0\u0a97\u0acd\u0aaf\u0acb \u0a9b\u0ac7. \u0a86\u0aaa \u0a95\u0ac7\u0ab5\u0ac0 \u0ab0\u0ac0\u0aa4\u0ac7 \u0aae\u0aa6\u0aa6 \u0a95\u0ab0\u0ac0 \u0ab6\u0a95\u0ac1\u0a82? (Hello! Uday Auto Links AI Voice Agent speaking. How can I help you?)',
    'fallback': 'I didn\'t quite catch that. Could you please repeat? / \u0aae\u0abe\u0ab0\u0acd\u0aab \u0a95\u0ab0\u0ab6\u0acb, \u0aab\u0ab0\u0ac0 \u0a95\u0ab9\u0acb?',
    'escalation': 'Let me transfer you to a senior team member who can assist further. Please hold on.',
    'closing': 'Thank you for calling Uday Auto Links! Have a great day! / \u0a95\u0ac9\u0ab2 \u0a95\u0ab0\u0ab5\u0abe \u0aac\u0aa6\u0ab2 \u0a86\u0aad\u0abe\u0ab0!',
    'compliance': 'This call may be recorded for quality and training purposes. / \u0a86 \u0a95\u0ac9\u0ab2 \u0a97\u0ac1\u0aa3\u0ab5\u0aa4\u0acd\u0aa4\u0abe \u0a85\u0aa8\u0ac7 \u0aa4\u0abe\u0ab2\u0abf\u0aae \u0aae\u0abe\u0a9f\u0ac7 \u0ab0\u0ac0\u0a95\u0ac9\u0ab0\u0acd\u0aa1 \u0a95\u0ab0\u0ab5\u0abe\u0aae\u0abe\u0a82 \u0a86\u0ab5\u0ac7 \u0a9b\u0ac7.',
    'variables': '{\n  "company": "Uday Auto Links",\n  "agent_name": "AI Voice Agent",\n  "primary_language": "Gujarati",\n  "secondary_languages": ["English", "Hindi"],\n  "timezone": "Asia/Kolkata",\n  "business_hours": "9:00 AM - 7:00 PM Mon-Sat",\n  "emergency_hours": "24x7 Roadside Assistance"\n}',
    'version': 'v3.2', 'published': True, 'draft': False,
    'history': [
        {'version': 'v3.2', 'date': '2026-07-20', 'author': 'Admin', 'status': 'Published', 'changes': 'Added Gujarati-first language instruction, updated mirroring logic'},
        {'version': 'v3.1', 'date': '2026-07-10', 'author': 'Admin', 'status': 'Published', 'changes': 'Improved sentiment detection, added compliance prompt'},
        {'version': 'v3.0', 'date': '2026-06-25', 'author': 'Admin', 'status': 'Published', 'changes': 'Major rewrite for multilingual support'},
        {'version': 'v2.5', 'date': '2026-06-01', 'author': 'Admin', 'status': 'Archived', 'changes': 'Added fallback and escalation prompts'},
        {'version': 'v2.0', 'date': '2026-05-15', 'author': 'Admin', 'status': 'Archived', 'changes': 'Initial system prompt structure'},
    ]
}

RAG_DOCUMENTS = [
    {'id': 'd1', 'title': 'Company Profile.pdf', 'desc': 'Uday Auto Links company overview and history', 'pages': 12, 'size': '2.4 MB', 'uploaded': '2026-07-15', 'indexed': True, 'source': 'upload', 'chunks': 48, 'status': 'Active', 'vector_count': 384, 'health': 'Healthy'},
    {'id': 'd2', 'title': 'Pricing 2026.pdf', 'desc': 'Service pricing, packages, and offers', 'pages': 8, 'size': '1.8 MB', 'uploaded': '2026-07-14', 'indexed': True, 'source': 'upload', 'chunks': 32, 'status': 'Active', 'vector_count': 256, 'health': 'Healthy'},
    {'id': 'd3', 'title': 'Refund Policy.pdf', 'desc': 'Cancellation and refund terms', 'pages': 5, 'size': '0.9 MB', 'uploaded': '2026-07-13', 'indexed': True, 'source': 'upload', 'chunks': 20, 'status': 'Active', 'vector_count': 160, 'health': 'Healthy'},
    {'id': 'd4', 'title': 'Maruti Suzuki FAQ.pdf', 'desc': 'Frequently asked questions and answers', 'pages': 15, 'size': '3.1 MB', 'uploaded': '2026-07-12', 'indexed': True, 'source': 'upload', 'chunks': 60, 'status': 'Active', 'vector_count': 480, 'health': 'Healthy'},
    {'id': 'd5', 'title': 'Service Guide.pdf', 'desc': 'Step-by-step service process guide', 'pages': 22, 'size': '4.2 MB', 'uploaded': '2026-07-11', 'indexed': True, 'source': 'upload', 'chunks': 88, 'status': 'Active', 'vector_count': 704, 'health': 'Healthy'},
    {'id': 'd6', 'title': 'Vehicle Specs.pdf', 'desc': 'Technical specifications for all Maruti Suzuki models', 'pages': 45, 'size': '6.8 MB', 'uploaded': '2026-07-10', 'indexed': True, 'source': 'upload', 'chunks': 180, 'status': 'Active', 'vector_count': 1440, 'health': 'Healthy'},
    {'id': 'd7', 'title': 'Sales Brochure 2026.pdf', 'desc': 'Marketing collateral and service packages', 'pages': 10, 'size': '5.2 MB', 'uploaded': '2026-07-09', 'indexed': True, 'source': 'upload', 'chunks': 40, 'status': 'Active', 'vector_count': 320, 'health': 'Healthy'},
    {'id': 'd8', 'title': 'Workshop Manual.pdf', 'desc': 'Service center operations manual', 'pages': 18, 'size': '3.5 MB', 'uploaded': '2026-07-18', 'indexed': False, 'source': 'upload', 'chunks': 72, 'status': 'Processing', 'vector_count': 0, 'health': 'Indexing'},
    {'id': 'd9', 'title': 'Warranty Terms.pdf', 'desc': 'Standard and extended warranty terms', 'pages': 7, 'size': '1.2 MB', 'uploaded': '2026-07-17', 'indexed': True, 'source': 'upload', 'chunks': 28, 'status': 'Active', 'vector_count': 224, 'health': 'Healthy'},
    {'id': 'd10', 'title': 'Contact List.csv', 'desc': 'Customer support contact directory', 'pages': 3, 'size': '0.3 MB', 'uploaded': '2026-07-16', 'indexed': True, 'source': 'upload', 'chunks': 12, 'status': 'Active', 'vector_count': 96, 'health': 'Healthy'},
    {'id': 'd11', 'title': 'arenaofkathwada.com/faq', 'desc': 'Website FAQ scraped knowledge', 'pages': '--', 'size': '--', 'uploaded': '2026-07-19', 'indexed': True, 'source': 'website', 'chunks': 24, 'status': 'Active', 'vector_count': 192, 'health': 'Healthy'},
    {'id': 'd12', 'title': 'arenaofkathwada.com/services', 'desc': 'Service pages from website', 'pages': '--', 'size': '--', 'uploaded': '2026-07-19', 'indexed': True, 'source': 'website', 'chunks': 36, 'status': 'Active', 'vector_count': 288, 'health': 'Healthy'},
]

RAG_STORAGE = os.path.join(os.path.dirname(__file__), 'rag_docs')
os.makedirs(RAG_STORAGE, exist_ok=True)

CALLBACK_QUEUE = []

SETTINGS = {
    'voice': {'model': 'GPT-5.5', 'voice_id': 'priya_v3', 'voice_name': 'Indian Female', 'language': 'English + Hindi + Gujarati', 'speed': 1.0, 'pitch': 1.0},
    'llm': {'model': 'GPT-5.5', 'temperature': 0.3, 'max_tokens': 256, 'top_p': 0.95, 'frequency_penalty': 0.1, 'presence_penalty': 0.1},
    'latency': {'endpointing_ms': 450, 'vad_threshold': 0.6, 'noise_suppression': True, 'echo_cancellation': True, 'max_call_duration': 600},
    'streaming': {'enabled': True, 'auto_continue': True, 'interruption_sensitivity': 'High', 'barge_in': True, 'playout_buffer_ms': 150},
    'memory': {'enabled': True, 'session_memory': True, 'cross_session_memory': True, 'max_history': 50, 'recall_window_days': 30},
    'recording': {'enabled': True, 'auto_record': True, 'storage_days': 90, 'transcript_enabled': True, 'sentiment_analysis': True, 'speaker_diarization': True},
    'security': {'encryption': 'AES-256', 'data_residency': 'India', 'hipaa': False, 'gdpr_compliant': True, 'pii_redaction': True, 'audit_log': True},
    'api': {'keys': [{'name': 'Production Key', 'key': 'sk-...a3f8', 'created': '2026-01-15', 'last_used': '2026-07-21', 'status': 'Active'}, {'name': 'Staging Key', 'key': 'sk-...b2c1', 'created': '2026-03-01', 'last_used': '2026-07-20', 'status': 'Active'}], 'rate_limit': 1000, 'usage_mtd': '95%'},
}

def compute_stats(calls):
    total = len(calls)
    completed = [c for c in calls if c['status'] == 'completed']
    failed = [c for c in calls if c['status'] == 'failed']
    answered = len(completed)
    missed = len([c for c in calls if c['outcome'] in ('No Response', 'Busy', 'Voicemail', 'Wrong Number')])
    avg_dur = sum(c['duration_sec'] for c in completed) / max(answered, 1)
    rating_list = [c['rating'] for c in completed if c['rating'] > 0]
    avg_rating = round(sum(rating_list) / max(len(rating_list), 1), 1)
    total_min = sum(c['duration_sec'] for c in calls) / 60
    total_cost = sum(c['cost'] for c in calls)
    bookings = len([c for c in calls if c['outcome'] == 'Completed'])
    conversions = [c for c in calls if c['outcome'] == 'Completed']
    return {
        'total_calls': total, 'answered': answered, 'missed': missed,
        'avg_duration_sec': round(avg_dur), 'avg_duration': f'{int(avg_dur//60)}m {int(avg_dur%60)}s',
        'avg_response_ms': 480, 'satisfaction': 96, 'avg_rating': avg_rating,
        'ai_cost': round(total_cost, 2), 'voice_minutes': round(total_min, 1),
        'concurrent_calls': 12, 'success_rate': round(answered / max(total, 1) * 100, 1),
        'total_bookings': bookings, 'booking_rate': round(bookings / max(total, 1) * 100, 1),
        'callback_rate': round(len([c for c in calls if c['callback_required']]) / max(total, 1) * 100, 1),
        'fail_rate': round(len(failed) / max(total, 1) * 100, 1),
        'whatsapp_sent': random.randint(40, 80), 'email_sent': random.randint(20, 50),
        'total_called': answered, 'avg_confidence': round(sum(c['ai_confidence'] for c in completed) / max(answered, 1), 2),
        # Automotive-specific KPIs
        'vehicles_in_service': random.randint(18, 35),
        'vehicles_ready': random.randint(5, 15),
        'workshop_load': random.randint(65, 90),
        'parts_inventory': random.randint(85, 98),
        'advisor_performance': random.randint(88, 98),
        'today_bookings': random.randint(8, 22),
        'waiting_queue': random.randint(3, 12),
        'active_calls': random.randint(3, 8),
        'callback_queue': random.randint(2, 6),
        'ai_status': 'Online',
        'live_conversations': random.randint(2, 6),
        'spare_parts_alerts': random.randint(0, 3),
        'workshop_occupancy': random.randint(70, 95),
        'avg_wait_time': '2m 30s',
        'missed_calls': random.randint(3, 15),
        'ai_confidence': 94,
        'callback_success': random.randint(75, 95),
        'avg_response_ms': random.randint(350, 650),
        'available_advisors': random.randint(3, 6),
        'technicians_active': random.randint(8, 15),
        'pending_job_cards': random.randint(3, 10),
        'service_bays': random.randint(6, 12),
        'pickup_scheduled': random.randint(1, 4),
        'drop_scheduled': random.randint(2, 5),
        'customer_satisfaction': random.randint(88, 97),
    }

def generate_live_calls():
    return [
        {'id': 'live_1', 'name': 'Priya Sharma', 'phone': '+91 98765 12345', 'duration': '3m 42s', 'status': 'Talking...', 'intent': 'Service Booking', 'sentiment': 'Satisfied', 'vehicle': 'Swift', 'language': 'Gujarati', 'waveform': True, 'direction': 'inbound', 'number': PHONE_1},
        {'id': 'live_2', 'name': 'Rahul Verma', 'phone': '+91 99887 66554', 'duration': '1m 18s', 'status': 'Listening...', 'intent': 'Complaint', 'sentiment': 'Annoyed', 'vehicle': 'Baleno', 'language': 'Hindi', 'waveform': True, 'direction': 'inbound', 'number': PHONE_1},
        {'id': 'live_3', 'name': 'Ananya Patel', 'phone': '+91 88776 65544', 'duration': '0m 45s', 'status': 'AI Responding...', 'intent': 'Inquiry', 'sentiment': 'Neutral', 'vehicle': 'Brezza', 'language': 'Gujarati', 'waveform': True, 'direction': 'outbound', 'number': PHONE_2},
        {'id': 'live_4', 'name': 'Vikram Singh', 'phone': '+91 77665 54433', 'duration': '5m 10s', 'status': 'Call Ending...', 'intent': 'Test Drive Booking', 'sentiment': 'Excited', 'vehicle': 'Grand Vitara', 'language': 'English', 'waveform': True, 'direction': 'outbound', 'number': PHONE_2},
        {'id': 'live_5', 'name': 'Kavita Desai', 'phone': '+91 66554 43322', 'duration': '0m 0s', 'status': 'Incoming...', 'intent': 'Insurance Query', 'sentiment': '--', 'vehicle': 'Ertiga', 'language': '--', 'waveform': False, 'direction': 'inbound', 'number': PHONE_1},
        {'id': 'live_6', 'name': 'Nishant Joshi', 'phone': '+91 55443 32211', 'duration': '0m 0s', 'status': 'Outgoing...', 'intent': 'Follow-up', 'sentiment': '--', 'vehicle': 'Dzire', 'language': '--', 'waveform': False, 'direction': 'outbound', 'number': PHONE_2},
    ]

# ─── Routes ───

@app.route('/')
def root(): return redirect('/login')

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/console')
def console_page(): return render_template('console.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    if data.get('email') == 'admin@sandbox.com' and data.get('password') == 'admin123':
        return jsonify({'token': 'tok_admin', 'role': 'admin', 'name': 'Admin', 'email': 'admin@sandbox.com', 'dashboard_role': 'admin'})
    if data.get('email') == 'agent@sandbox.com' and data.get('password') == 'agent123':
        return jsonify({'token': 'tok_agent', 'role': 'agent', 'name': 'Agent', 'email': 'agent@sandbox.com', 'dashboard_role': 'agent'})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/me')
def api_me():
    return jsonify({'email': 'admin@sandbox.com', 'role': 'admin', 'name': 'Admin', 'dashboard_role': 'admin', 'locked': False})

@app.route('/api/agent')
def api_agent(): return jsonify(AGENT)

@app.route('/api/phone-numbers')
def api_phone_numbers(): return jsonify({'primary': PHONE_1, 'secondary': PHONE_2, 'numbers': [PHONE_1, PHONE_2]})

@app.route('/api/stats')
def api_stats(): return jsonify(compute_stats(CALLS))

@app.route('/api/calls')
def api_calls():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    search = request.args.get('search', '').lower()
    status_filter = request.args.get('status', '')
    sentiment_filter = request.args.get('sentiment', '')
    intent_filter = request.args.get('intent', '')
    lang_filter = request.args.get('language', '')
    direction_filter = request.args.get('direction', '')
    result = CALLS
    if search: result = [c for c in result if search in c['name'].lower() or search in c['phone'] or search in c['vehicle'].lower() or search in c['service_type'].lower()]
    if status_filter: result = [c for c in result if c['status'] == status_filter]
    if sentiment_filter: result = [c for c in result if c['sentiment'] == sentiment_filter]
    if intent_filter: result = [c for c in result if c['intent'] == intent_filter]
    if lang_filter: result = [c for c in result if c['language'] == lang_filter]
    if direction_filter: result = [c for c in result if c['direction'] == direction_filter]
    total = len(result)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_data = result[start:start+per_page]
    return jsonify({'calls': page_data, 'total': total, 'page': page, 'per_page': per_page, 'total_pages': total_pages})

@app.route('/api/calls/<int:call_id>')
def api_call_detail(call_id):
    call = next((c for c in CALLS if c['id'] == call_id), None)
    if not call: return jsonify({'error': 'Not found'}), 404
    return jsonify(call)

@app.route('/api/calls/<int:call_id>/recording')
def api_call_recording(call_id):
    import io, struct, math
    sample_rate = 8000
    duration = random.uniform(45, 180)
    num_samples = int(sample_rate * duration)
    buf = io.BytesIO()
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 36 + num_samples))
    buf.write(b'WAVEfmt ')
    buf.write(struct.pack('<IHHIIHH', 16, 1, 1, sample_rate, sample_rate, 1, 8))
    buf.write(b'data')
    buf.write(struct.pack('<I', num_samples))
    for i in range(num_samples):
        t = i / sample_rate
        sample = int(127 + 64 * math.sin(2 * math.pi * 440 * t))
        buf.write(struct.pack('B', sample))
    buf.seek(0)
    return Response(buf.read(), mimetype='audio/wav')

@app.route('/api/calls/<int:call_id>/reanalyze', methods=['POST'])
def api_reanalyze(call_id): return jsonify({'status': 'ok', 'message': 'Re-analysis queued'})

@app.route('/api/analytics')
def api_analytics():
    hours = list(range(24))
    hourly_data = [{'hour': h, 'count': len([c for c in CALLS if int(c['created_at'][11:13]) == h])} for h in hours]
    days = list(range(7))
    weekday_map = {0:'Mon',1:'Tue',2:'Wed',3:'Thu',4:'Fri',5:'Sat',6:'Sun'}
    weekday_data = [{'day': weekday_map[d], 'count': len([c for c in CALLS if datetime.datetime.strptime(c['created_at'], '%Y-%m-%dT%H:%M:%SZ').weekday() == d])} for d in days]
    lang_dist = {}
    for c in CALLS: lang_dist[c['language']] = lang_dist.get(c['language'], 0) + 1
    sentiment_dist = {}
    for c in CALLS: sentiment_dist[c['sentiment']] = sentiment_dist.get(c['sentiment'], 0) + 1
    daily_calls = {}
    for c in CALLS:
        d = c['created_at'][:10]
        daily_calls[d] = daily_calls.get(d, 0) + 1
    daily_chart = [{'date': d, 'count': daily_calls[d]} for d in sorted(daily_calls.keys())[-30:]]
    intent_dist = {}
    for c in CALLS: intent_dist[c['intent']] = intent_dist.get(c['intent'], 0) + 1
    cost_data = {}
    for c in CALLS:
        d = c['created_at'][:10]
        cost_data[d] = cost_data.get(d, 0) + c['cost']
    cost_chart = [{'date': d, 'cost': round(cost_data[d], 2)} for d in sorted(cost_data.keys())[-30:]]
    return jsonify({
        'hourly': hourly_data, 'weekday': weekday_data, 'daily': daily_chart,
        'languages': [{'name': k, 'count': v} for k,v in sorted(lang_dist.items(), key=lambda x:-x[1])],
        'sentiments': [{'name': k, 'count': v} for k,v in sorted(sentiment_dist.items(), key=lambda x:-x[1])],
        'intents': [{'name': k, 'count': v} for k,v in sorted(intent_dist.items(), key=lambda x:-x[1])],
        'costs': cost_chart,
        'peak_hour': max(hourly_data, key=lambda x: x['count']),
        'avg_response_time': 480, 'resolution_rate': 92.5,
        'total_transfers': random.randint(15, 30),
    })

@app.route('/api/tuning')
def api_tuning():
    return jsonify({'greeting': PROMPTS['greeting'], 'prompt': PROMPTS['system'], 'rag': '', 'prompts': PROMPTS})

@app.route('/api/tuning/greeting', methods=['POST'])
def api_tuning_greeting():
    data = request.get_json() or {}
    if 'greeting' in data: PROMPTS['greeting'] = data['greeting']
    return jsonify({'status': 'ok', 'greeting': PROMPTS['greeting']})

@app.route('/api/tuning/prompt', methods=['POST'])
def api_tuning_prompt():
    data = request.get_json() or {}
    if 'prompt' in data: PROMPTS['system'] = data['prompt']
    if 'field' in data: PROMPTS[data['field']] = data.get('value', '')
    return jsonify({'status': 'ok', 'prompt': PROMPTS})

@app.route('/api/rag')
def api_rag():
    return jsonify({
        'documents': RAG_DOCUMENTS,
        'total_docs': len(RAG_DOCUMENTS),
        'total_chunks': sum(d['chunks'] for d in RAG_DOCUMENTS),
        'total_vectors': sum(d['vector_count'] for d in RAG_DOCUMENTS),
        'indexed': len([d for d in RAG_DOCUMENTS if d['indexed']]),
        'last_updated': '2026-07-19T15:30:00Z',
        'health': 'Healthy',
    })

@app.route('/api/rag/search', methods=['POST'])
def api_rag_search():
    q = (request.get_json() or {}).get('query', '').lower()
    results = []
    for d in RAG_DOCUMENTS:
        if q in d['title'].lower() or q in d['desc'].lower():
            results.append({**d, 'score': round(random.uniform(0.6, 0.99), 2)})
    return jsonify({'results': sorted(results, key=lambda x: -x['score']), 'query': q})

@app.route('/api/rag/sync', methods=['POST'])
def api_rag_sync():
    return jsonify({'status': 'ok', 'message': 'Sync started', 'indexed': len([d for d in RAG_DOCUMENTS if d['indexed']]), 'total': len(RAG_DOCUMENTS)})

@app.route('/api/rag/retrain', methods=['POST'])
def api_rag_retrain():
    return jsonify({'status': 'ok', 'message': 'Retraining started', 'estimated_time': '2 minutes'})

@app.route('/api/notifications')
def api_notifications():
    return jsonify({'notifications': NOTIFICATIONS, 'unread': len([n for n in NOTIFICATIONS if not n['read']])})

@app.route('/api/notifications/<nid>/read', methods=['POST'])
def api_notification_read(nid):
    for n in NOTIFICATIONS:
        if n['id'] == nid: n['read'] = True
    return jsonify({'status': 'ok'})

@app.route('/api/settings')
def api_settings(): return jsonify(SETTINGS)

@app.route('/api/settings', methods=['POST'])
def api_settings_update():
    data = request.get_json() or {}
    for section, values in data.items():
        if section in SETTINGS:
            if isinstance(values, dict):
                SETTINGS[section].update(values)
            else:
                SETTINGS[section] = values
    return jsonify({'status': 'ok', 'settings': SETTINGS})

@app.route('/api/live')
def api_live():
    return jsonify({'calls': generate_live_calls(), 'active_count': len(generate_live_calls())})

@app.route('/api/events/stream')
def api_events_stream():
    import time
    def generate():
        i = 0
        while True:
            yield f'data: {json.dumps({"type":"stats_update","stats":compute_stats(CALLS),"timestamp":datetime.datetime.now().isoformat()})}\n\n'
            time.sleep(5)
            i += 1
            if i > 100: break
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/conversations')
def api_conversations():
    page = request.args.get('page', 1, type=int)
    per_page = 15
    conversations = []
    for c in CALLS[:60]:
        if c['transcript']:
            conversations.append({
                'id': c['id'], 'name': c['name'], 'phone': c['phone'],
                'transcript': c['transcript'], 'sentiment': c['sentiment'],
                'language': c['language'], 'rating': c['rating'],
                'ai_confidence': c['ai_confidence'], 'intent': c['intent'],
                'vehicle': c['vehicle'], 'duration': c['duration'],
                'date': c['date'], 'summary': c['notes'],
                'topics': [c['intent'], c['service_type']],
            })
    total = len(conversations)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    return jsonify({'conversations': conversations[start:start+per_page], 'total': total, 'page': page, 'total_pages': total_pages})

@app.route('/api/dashboard')
def api_dashboard():
    stats = compute_stats(CALLS)
    recent = sorted(CALLS, key=lambda x: x['created_at'], reverse=True)[:10]
    return jsonify({'stats': stats, 'recent_calls': recent, 'agent': AGENT, 'notifications': NOTIFICATIONS[:5]})

@app.route('/api/schedules')
def api_schedules(): return jsonify({'schedules': []})

@app.route('/api/callbacks')
def api_callbacks(): return jsonify({'callbacks': [c for c in CALLS if c['callback_required']][:10]})

@app.route('/api/manual/call')
def api_manual_call(): return jsonify({'status': 'ready', 'numbers': [PHONE_1, PHONE_2]})

@app.route('/api/rag/upload', methods=['POST'])
def api_rag_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = f.filename
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    allowed = {'pdf', 'docx', 'xlsx', 'csv', 'txt'}
    if ext not in allowed:
        return jsonify({'error': f'Unsupported file type: .{ext}. Allowed: {", ".join(allowed)}'}), 400
    filepath = os.path.join(RAG_STORAGE, filename)
    f.save(filepath)
    size_mb = round(os.path.getsize(filepath) / (1024*1024), 1)
    new_id = f'd{len(RAG_DOCUMENTS) + 1}'
    num_chunks = random.randint(10, 60)
    new_doc = {
        'id': new_id, 'title': filename, 'desc': f'Uploaded document - {filename}',
        'pages': '--', 'size': f'{size_mb} MB', 'uploaded': datetime.datetime.now().strftime('%Y-%m-%d'),
        'indexed': True, 'source': 'upload', 'chunks': num_chunks, 'status': 'Active',
        'vector_count': num_chunks * 8, 'health': 'Healthy'
    }
    RAG_DOCUMENTS.insert(0, new_doc)
    return jsonify({'status': 'ok', 'message': f'{filename} uploaded and indexed', 'doc': new_doc})

@app.route('/api/rag/<doc_id>/delete', methods=['POST'])
def api_rag_delete(doc_id):
    global RAG_DOCUMENTS
    RAG_DOCUMENTS = [d for d in RAG_DOCUMENTS if d['id'] != doc_id]
    return jsonify({'status': 'ok', 'message': 'Document deleted'})

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').lower().strip()
    if not q or len(q) < 2:
        return jsonify({'results': []})
    results = []
    # Search calls
    for c in CALLS[:200]:
        score = 0
        if q in c['name'].lower(): score += 10
        if q in c['phone']: score += 10
        if q in c['vehicle'].lower(): score += 8
        if q in c['service_type'].lower(): score += 5
        if q in c['intent'].lower(): score += 5
        if q in c['notes'].lower(): score += 3
        if score > 0:
            results.append({'type': 'Call', 'id': c['id'], 'title': c['name'], 'subtitle': f"{c['vehicle']} · {c['intent']} · {c['date']}", 'url': '#', 'score': score})
    # Search RAG docs
    for d in RAG_DOCUMENTS:
        score = 0
        if q in d['title'].lower(): score += 10
        if q in d['desc'].lower(): score += 6
        if score > 0:
            results.append({'type': 'Document', 'id': d['id'], 'title': d['title'], 'subtitle': d['desc'], 'url': '#', 'score': score})
    # Search conversations (using call transcripts)
    for c in CALLS[:100]:
        if c.get('transcript') and q in c['transcript'].lower():
            results.append({'type': 'Conversation', 'id': c['id'], 'title': f"Conversation with {c['name']}", 'subtitle': f"{c['intent']} · {c['date']}", 'url': '#', 'score': 5})
    results.sort(key=lambda x: -x['score'])
    return jsonify({'results': results[:20], 'query': q})

@app.route('/api/callbacks/enqueue', methods=['POST'])
def api_callback_enqueue():
    data = request.get_json() or {}
    name = data.get('name', 'Unknown')
    phone = data.get('phone', 'Unknown')
    reason = data.get('reason', 'Callback requested')
    cb = {
        'id': f'cb_{uuid.uuid4().hex[:8]}',
        'name': name, 'phone': phone, 'reason': reason,
        'requested_at': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'status': 'pending', 'priority': data.get('priority', 'normal'),
        'notes': data.get('notes', ''),
        'callback_attempts': 0,
    }
    CALLBACK_QUEUE.append(cb)
    return jsonify({'status': 'ok', 'callback': cb})

@app.route('/api/callbacks/queue')
def api_callback_queue():
    # Merge with existing callback_required calls
    callback_calls = [{'id': c['id'], 'name': c['name'], 'phone': c['phone'], 'reason': c.get('notes', 'Missed call'), 'requested_at': c['created_at'], 'status': 'pending', 'priority': 'normal', 'callback_attempts': 0} for c in CALLS if c.get('callback_required')]
    all_q = CALLBACK_QUEUE + callback_calls
    return jsonify({'queue': all_q, 'total': len(all_q)})

@app.route('/api/campaigns')
def api_campaigns():
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')
    result = CAMPAIGNS
    if status_filter: result = [c for c in result if c['status'] == status_filter]
    if type_filter: result = [c for c in result if c['type'] == type_filter]
    stats = {
        'total': len(result), 'running': len([c for c in result if c['status'] == 'Running']),
        'completed': len([c for c in result if c['status'] == 'Completed']),
        'scheduled': len([c for c in result if c['status'] == 'Scheduled']),
        'paused': len([c for c in result if c['status'] == 'Paused']),
        'failed_campaigns': len([c for c in result if c['status'] == 'Failed']),
        'total_calls': sum(c['total'] for c in result),
        'total_connected': sum(c['connected'] for c in result),
        'total_interested': sum(c['interested'] for c in result),
        'total_cost': round(sum(c['cost'] for c in result), 2),
    }
    return jsonify({'campaigns': result, 'stats': stats})

@app.route('/api/campaigns/<int:campaign_id>')
def api_campaign_detail(campaign_id):
    c = next((c for c in CAMPAIGNS if c['id'] == campaign_id), None)
    if not c: return jsonify({'error': 'Not found'}), 404
    return jsonify(c)

@app.route('/api/campaigns/<int:campaign_id>/start', methods=['POST'])
def api_campaign_start(campaign_id):
    c = next((c for c in CAMPAIGNS if c['id'] == campaign_id), None)
    if c: c['status'] = 'Running'
    return jsonify({'status': 'ok', 'message': 'Campaign started'})

@app.route('/api/campaigns/<int:campaign_id>/pause', methods=['POST'])
def api_campaign_pause(campaign_id):
    c = next((c for c in CAMPAIGNS if c['id'] == campaign_id), None)
    if c: c['status'] = 'Paused'
    return jsonify({'status': 'ok', 'message': 'Campaign paused'})

@app.route('/api/campaigns/<int:campaign_id>/resume', methods=['POST'])
def api_campaign_resume(campaign_id):
    c = next((c for c in CAMPAIGNS if c['id'] == campaign_id), None)
    if c: c['status'] = 'Running'
    return jsonify({'status': 'ok', 'message': 'Campaign resumed'})

@app.route('/api/campaigns/<int:campaign_id>/stop', methods=['POST'])
def api_campaign_stop(campaign_id):
    c = next((c for c in CAMPAIGNS if c['id'] == campaign_id), None)
    if c: c['status'] = 'Completed'
    return jsonify({'status': 'ok', 'message': 'Campaign stopped'})

@app.route('/api/campaigns/templates')
def api_campaign_templates():
    return jsonify({'templates': [
        {'id': 1, 'name': 'Standard Service Reminder', 'type': 'Service Due', 'script': 'Namaste! This is PitchXAI calling from Uday Auto Links. Your {vehicle} is due for {service}. Would you like to book a slot?', 'language': 'Multilingual'},
        {'id': 2, 'name': 'Festive Offer Announcement', 'type': 'Festival Offer', 'script': 'Namaste! Uday Auto Links has exclusive festive offers on {service}. Save up to {discount}%!', 'language': 'Multilingual'},
        {'id': 3, 'name': 'Insurance Renewal Reminder', 'type': 'Insurance Renewal', 'script': 'Namaste! Your insurance for {vehicle} is expiring soon. Renew now with Uday Auto Links.', 'language': 'Multilingual'},
        {'id': 4, 'name': 'Feedback Collection', 'type': 'Feedback', 'script': 'Namaste! Uday Auto Links here. How was your recent service experience? We value your feedback.', 'language': 'Multilingual'},
    ]})

CAMPAIGN_FILES = []

# ─── CAMPAIGN WORKER ───
CAMPAIGN_LEADS = []
CAMPAIGN_STATE = {
    'running': False, 'paused': False, 'leads_total': 0, 'leads_called': 0,
    'leads_interested': 0, 'leads_failed': 0, 'leads_callback': 0,
    'current_lead': None, 'call_rate': 0, 'inter_call_gap': 5,
    'quiet_hours_start': 9, 'quiet_hours_end': 19,
}
CAMPAIGN_NOTIFICATIONS = []
_campaign_worker_thread = None

def generate_leads(count):
    global CAMPAIGN_LEADS
    CAMPAIGN_LEADS = []
    for i in range(count):
        name = random.choice(FIRST_NAMES) + ' ' + random.choice(FIRST_NAMES)
        phone = generate_phone()
        vehicle = random.choice(VEHICLES)
        service = random.choice(SERVICES)
        CAMPAIGN_LEADS.append({
            'id': f'lead_{uuid.uuid4().hex[:8]}',
            'name': name, 'phone': phone, 'vehicle': vehicle,
            'service_type': service, 'status': 'pending',
            'attempts': 0, 'last_disposition': None,
            'callback_time': None, 'callback_reminder_epoch': None,
            'created_at': datetime.datetime.now().isoformat(),
            'updated_at': datetime.datetime.now().isoformat(),
        })

def add_campaign_notification(ntype, title, desc):
    n = {
        'id': f'cn_{uuid.uuid4().hex[:8]}', 'type': ntype,
        'title': title, 'desc': desc,
        'time': datetime.datetime.now().strftime('%H:%M:%S'),
        'read': False, 'icon': 'campaign' if ntype == 'info' else 'check_circle' if ntype == 'success' else 'error'
    }
    CAMPAIGN_NOTIFICATIONS.insert(0, n)
    if len(CAMPAIGN_NOTIFICATIONS) > 50:
        CAMPAIGN_NOTIFICATIONS.pop()

def simulate_call(lead):
    lead['attempts'] += 1
    lead['updated_at'] = datetime.datetime.now().isoformat()
    disposition = random.choice(['completed', 'completed', 'completed', 'completed', 'failed', 'no_response', 'busy', 'callback_requested'])
    if disposition == 'completed':
        lead['status'] = 'completed'
        lead['last_disposition'] = 'Completed'
        CAMPAIGN_STATE['leads_interested'] += 1
        add_campaign_notification('success', f'Call Completed', f'{lead["name"]} ({lead["vehicle"]}) - {lead["service_type"]}')
    elif disposition == 'callback_requested':
        lead['status'] = 'callback_scheduled'
        lead['last_disposition'] = 'Callback Scheduled'
        lead['callback_time'] = (datetime.datetime.now() + datetime.timedelta(hours=random.randint(1,4))).isoformat()
        lead['callback_reminder_epoch'] = lead['callback_time']
        CAMPAIGN_STATE['leads_callback'] += 1
        add_campaign_notification('info', f'Callback Scheduled', f'{lead["name"]} requested callback')
    elif disposition == 'busy':
        lead['status'] = 'busy'
        lead['last_disposition'] = 'Busy'
        CAMPAIGN_STATE['leads_failed'] += 1
    else:
        lead['status'] = 'no_response'
        lead['last_disposition'] = 'No Response'
        CAMPAIGN_STATE['leads_failed'] += 1
    return disposition

def campaign_worker_loop():
    global _campaign_worker_thread
    phone_index = 0
    phones = [PHONE_1, PHONE_2]
    while CAMPAIGN_STATE['running']:
        if CAMPAIGN_STATE['paused']:
            _time.sleep(1)
            continue
        now = datetime.datetime.now()
        if now.hour < CAMPAIGN_STATE['quiet_hours_start'] or now.hour >= CAMPAIGN_STATE['quiet_hours_end']:
            _time.sleep(10)
            continue
        pending = [l for l in CAMPAIGN_LEADS if l['status'] in ('pending', 'busy', 'no_response', 'callback_scheduled')]
        if not pending:
            add_campaign_notification('info', 'Campaign Complete', 'All leads have been processed')
            CAMPAIGN_STATE['running'] = False
            break
        lead = pending[0]
        CAMPAIGN_STATE['current_lead'] = {
            'id': lead['id'], 'name': lead['name'], 'phone': lead['phone'],
            'vehicle': lead['vehicle'], 'status': 'calling',
            'outbound_number': phones[phone_index % len(phones)]
        }
        phone_index += 1
        _time.sleep(2)
        disposition = simulate_call(lead)
        CAMPAIGN_STATE['leads_called'] += 1
        CAMPAIGN_STATE['current_lead'] = None
        gap = CAMPAIGN_STATE['inter_call_gap'] + random.uniform(-1, 2)
        _time.sleep(max(1, gap))

@app.route('/api/campaigns/files')
def api_campaign_files():
    return jsonify({'files': CAMPAIGN_FILES, 'total': len(CAMPAIGN_FILES)})

@app.route('/api/campaigns/files/upload', methods=['POST'])
def api_campaign_file_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    filename = f.filename
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in {'csv', 'xlsx', 'xls'}:
        return jsonify({'error': f'Unsupported file type: .{ext}. Allowed: csv, xlsx, xls'}), 400
    filepath = os.path.join(RAG_STORAGE, filename)
    f.save(filepath)
    size_kb = round(os.path.getsize(filepath) / 1024, 1)
    leads = random.randint(50, 500)
    file_entry = {
        'id': f'cf_{uuid.uuid4().hex[:8]}', 'name': filename,
        'size': f'{size_kb} KB', 'leads': leads,
        'uploaded': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), 'status': 'Loaded'
    }
    CAMPAIGN_FILES.insert(0, file_entry)
    return jsonify({'status': 'ok', 'file': file_entry})

@app.route('/api/campaigns/control', methods=['POST'])
def api_campaign_control():
    global _campaign_worker_thread
    data = request.get_json() or {}
    action = data.get('action', '')
    if action == 'start':
        if CAMPAIGN_STATE['running']:
            return jsonify({'status': 'ok', 'message': 'Campaign already running', 'running': True})
        if not CAMPAIGN_LEADS:
            generate_leads(random.randint(200, 500))
        CAMPAIGN_STATE['running'] = True
        CAMPAIGN_STATE['paused'] = False
        CAMPAIGN_STATE['leads_total'] = len(CAMPAIGN_LEADS)
        CAMPAIGN_STATE['leads_called'] = 0
        CAMPAIGN_STATE['leads_interested'] = 0
        CAMPAIGN_STATE['leads_failed'] = 0
        CAMPAIGN_STATE['leads_callback'] = 0
        add_campaign_notification('success', 'Campaign Started', f'Dialing {len(CAMPAIGN_LEADS)} leads')
        _campaign_worker_thread = threading.Thread(target=campaign_worker_loop, daemon=True)
        _campaign_worker_thread.start()
        return jsonify({'status': 'ok', 'message': 'Campaign started', 'running': True, 'total_leads': len(CAMPAIGN_LEADS)})
    elif action == 'stop':
        CAMPAIGN_STATE['running'] = False
        CAMPAIGN_STATE['paused'] = False
        CAMPAIGN_STATE['current_lead'] = None
        add_campaign_notification('info', 'Campaign Stopped', f'Called {CAMPAIGN_STATE["leads_called"]}/{CAMPAIGN_STATE["leads_total"]} leads')
        return jsonify({'status': 'ok', 'message': 'Campaign stopped', 'running': False})
    elif action == 'pause':
        CAMPAIGN_STATE['paused'] = True
        add_campaign_notification('info', 'Campaign Paused', 'Outbound dialing paused')
        return jsonify({'status': 'ok', 'message': 'Campaign paused', 'running': False})
    elif action == 'resume':
        CAMPAIGN_STATE['paused'] = False
        add_campaign_notification('info', 'Campaign Resumed', 'Outbound dialing resumed')
        return jsonify({'status': 'ok', 'message': 'Campaign resumed', 'running': True})
    elif action == 'reanalyze':
        return jsonify({'status': 'ok', 'message': 'Re-analyzing leads...'})
    elif action == 'clear':
        CAMPAIGN_STATE['running'] = False
        CAMPAIGN_STATE['paused'] = False
        CAMPAIGN_STATE['current_lead'] = None
        CAMPAIGN_STATE['leads_total'] = 0
        CAMPAIGN_STATE['leads_called'] = 0
        CAMPAIGN_STATE['leads_interested'] = 0
        CAMPAIGN_STATE['leads_failed'] = 0
        CAMPAIGN_STATE['leads_callback'] = 0
        CAMPAIGN_LEADS.clear()
        CAMPAIGN_FILES.clear()
        add_campaign_notification('info', 'Campaign Cleared', 'All leads and files cleared')
        return jsonify({'status': 'ok', 'message': 'All leads cleared'})
    elif action == 'set_pause':
        pause = data.get('pause_seconds', 5)
        CAMPAIGN_STATE['inter_call_gap'] = pause
        return jsonify({'status': 'ok', 'message': f'Pause set to {pause}s', 'pause_seconds': pause})
    return jsonify({'error': 'Invalid action'}), 400

@app.route('/api/campaigns/status')
def api_campaign_status():
    pending = len([l for l in CAMPAIGN_LEADS if l['status'] == 'pending'])
    calling = len([l for l in CAMPAIGN_LEADS if l['status'] == 'calling'])
    completed = len([l for l in CAMPAIGN_LEADS if l['status'] == 'completed'])
    failed = len([l for l in CAMPAIGN_LEADS if l['status'] in ('failed', 'no_response', 'busy')])
    callback = len([l for l in CAMPAIGN_LEADS if l['status'] == 'callback_scheduled'])
    return jsonify({
        'running': CAMPAIGN_STATE['running'], 'paused': CAMPAIGN_STATE['paused'],
        'leads_total': len(CAMPAIGN_LEADS), 'leads_pending': pending,
        'leads_calling': calling, 'leads_completed': completed,
        'leads_failed': failed, 'leads_callback': callback,
        'leads_called': CAMPAIGN_STATE['leads_called'],
        'current_lead': CAMPAIGN_STATE['current_lead'],
        'call_rate': CAMPAIGN_STATE['call_rate'],
        'inter_call_gap': CAMPAIGN_STATE['inter_call_gap'],
    })

@app.route('/api/campaigns/leads')
def api_campaign_leads():
    status_filter = request.args.get('status', '')
    result = CAMPAIGN_LEADS
    if status_filter:
        result = [l for l in result if l['status'] == status_filter]
    return jsonify({'leads': result[:100], 'total': len(CAMPAIGN_LEADS), 'filtered': len(result)})

@app.route('/api/campaigns/notifications')
def api_campaign_notifications():
    return jsonify({'notifications': CAMPAIGN_NOTIFICATIONS[:20], 'unread': len([n for n in CAMPAIGN_NOTIFICATIONS if not n['read']])})

@app.route('/api/campaigns/events')
def api_campaign_events():
    import time
    def generate():
        while True:
            pending = len([l for l in CAMPAIGN_LEADS if l['status'] == 'pending'])
            calling = len([l for l in CAMPAIGN_LEADS if l['status'] == 'calling'])
            completed = len([l for l in CAMPAIGN_LEADS if l['status'] == 'completed'])
            failed = len([l for l in CAMPAIGN_LEADS if l['status'] in ('failed', 'no_response', 'busy')])
            callback = len([l for l in CAMPAIGN_LEADS if l['status'] == 'callback_scheduled'])
            data = {
                'type': 'campaign_update',
                'running': CAMPAIGN_STATE['running'],
                'paused': CAMPAIGN_STATE['paused'],
                'leads_total': len(CAMPAIGN_LEADS),
                'leads_pending': pending, 'leads_calling': calling,
                'leads_completed': completed, 'leads_failed': failed,
                'leads_callback': callback,
                'current_lead': CAMPAIGN_STATE['current_lead'],
                'notifications': CAMPAIGN_NOTIFICATIONS[:5],
                'timestamp': datetime.datetime.now().isoformat()
            }
            yield f'data: {json.dumps(data)}\n\n'
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7070, debug=False)
