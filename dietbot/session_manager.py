#!/usr/bin/env python3
"""
session_manager.py – Manages chat sessions (max 5 per patient).
"""

import uuid
from typing import Dict, List, Optional
from datetime import datetime
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

MAX_SESSIONS_PER_PATIENT = 5

def get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)

class ChatSessionManager:
    def __init__(self):
        self.supabase = get_supabase()
        print("✅ ChatSessionManager initialized")

    def _count_sessions(self, patient_id: str) -> int:
        result = self.supabase.table('diet_chat_sessions') \
            .select('id', count='exact') \
            .eq('patient_id', patient_id) \
            .execute()
        return result.count or 0

    def _delete_oldest_session(self, patient_id: str):
        result = self.supabase.table('diet_chat_sessions') \
            .select('id') \
            .eq('patient_id', patient_id) \
            .order('created_at', asc=True) \
            .limit(1) \
            .execute()
        if result.data and len(result.data) > 0:
            oldest_id = result.data[0]['id']
            self.supabase.table('diet_chat_sessions') \
                .delete() \
                .eq('id', oldest_id) \
                .execute()
            print(f"🗑️ Deleted oldest session: {oldest_id}")

    def get_or_create_session(self, patient_id: str, session_id: Optional[str] = None) -> str:
        if session_id:
            result = self.supabase.table('diet_chat_sessions') \
                .select('id') \
                .eq('id', session_id) \
                .eq('patient_id', patient_id) \
                .execute()
            if result.data and len(result.data) > 0:
                return session_id

        count = self._count_sessions(patient_id)
        if count >= MAX_SESSIONS_PER_PATIENT:
            self._delete_oldest_session(patient_id)

        new_session_id = str(uuid.uuid4())
        self.supabase.table('diet_chat_sessions').insert({
            'id': new_session_id,
            'patient_id': patient_id,
            'title': f"Chat {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            'status': 'active'
        }).execute()
        print(f"✅ Created new session: {new_session_id}")
        return new_session_id

    def get_session_history(self, session_id: str, limit: int = 5) -> List[Dict]:
        result = self.supabase.table('diet_chat_messages') \
            .select('role, content, created_at') \
            .eq('session_id', session_id) \
            .order('created_at', desc=True) \
            .limit(limit) \
            .execute()
        messages = result.data if result.data else []
        messages.reverse()
        return messages

    def add_message(self, session_id: str, role: str, content: str):
        self.supabase.table('diet_chat_messages').insert({
            'session_id': session_id,
            'role': role,
            'content': content,
            'feedback_score': 0
        }).execute()
        print(f"✅ Message stored (role: {role})")

    def list_sessions(self, patient_id: str) -> List[Dict]:
        result = self.supabase.table('diet_chat_sessions') \
            .select('id, title, status, created_at') \
            .eq('patient_id', patient_id) \
            .order('created_at', desc=True) \
            .execute()
        return result.data if result.data else []

    def delete_session(self, session_id: str):
        self.supabase.table('diet_chat_sessions') \
            .delete() \
            .eq('id', session_id) \
            .execute()
        print(f"🗑️ Deleted session: {session_id}")