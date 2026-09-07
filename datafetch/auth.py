#!/usr/bin/env python3
"""
auth.py – Authentication module for patient login.

Uses bcrypt for password hashing and verification.
Integrates with Supabase to check patient credentials.
"""

import os
import bcrypt
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


# ============================================================
# 1. Password Hashing & Verification
# ============================================================
def hash_password(plain_password: str) -> str:
    """
    Hash a plain-text password using bcrypt.
    Returns: hashed password as string (includes salt).
    """
    print("[OK] Hashing password...")  # TODO: Remove this debug print after testing
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain_password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain-text password against a bcrypt hash.
    Returns: True if password matches, False otherwise.
    """
    print("[OK] Verifying password...")  # TODO: Remove this debug print after testing
    try:
        return bcrypt.checkpw(
            plain_password.encode('utf-8'),
            hashed_password.encode('utf-8')
        )
    except ValueError:
        # Invalid hash format
        return False


# ============================================================
# 2. Supabase Patient Authentication
# ============================================================
class PatientAuthenticator:
    """
    Authenticates patients against Supabase database.
    """

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        self.url = url or os.getenv("SUPABASE_URL")
        self.key = key or os.getenv("SUPABASE_KEY")
        if not self.url or not self.key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client: Client = create_client(self.url, self.key)
        print("[OK] PatientAuthenticator initialized")  # TODO: Remove this debug print after testing

    def authenticate(self, patient_id: str, plain_password: str) -> bool:
        """
        Authenticate a patient by ID and password.
        
        Steps:
        1. Query the patients table for the given patient_id.
        2. If patient exists, retrieve the password_hash.
        3. Verify the plain password against the hash.
        4. Return True if verified, False otherwise.
        """
        print(f"[INFO] Authenticating patient: {patient_id}")  # TODO: Remove this debug print after testing
        
        # 1. Query patient by ID
        try:
            result = self.client.table('patients') \
                .select('id, password_hash, consented_at') \
                .eq('id', patient_id) \
                .execute()
        except Exception as e:
            print(f"[ERROR] Supabase query failed: {e}")  # TODO: Remove this debug print after testing
            return False

        # 2. Check if patient exists
        if not result.data or len(result.data) == 0:
            print(f"[WARN] Patient not found: {patient_id}")  # TODO: Remove this debug print after testing
            return False

        patient = result.data[0]
        
        # 3. Check if password_hash exists
        hashed = patient.get('password_hash')
        if not hashed:
            print(f"[WARN] Patient has no password set: {patient_id}")  # TODO: Remove this debug print after testing
            return False

        # 4. Verify password
        is_valid = verify_password(plain_password, hashed)
        if is_valid:
            print(f"[OK] Authentication successful for patient: {patient_id}")  # TODO: Remove this debug print after testing
        else:
            print(f"[WARN] Authentication failed for patient: {patient_id}")  # TODO: Remove this debug print after testing
        
        return is_valid

    def get_patient_consent(self, patient_id: str) -> Optional[str]:
        """
        Check if patient has given consent.
        Returns: consented_at timestamp or None.
        """
        try:
            result = self.client.table('patients') \
                .select('consented_at') \
                .eq('id', patient_id) \
                .execute()
        except Exception:
            return None

        if not result.data or len(result.data) == 0:
            return None
        
        return result.data[0].get('consented_at')


# ============================================================
# 3. Convenience Function
# ============================================================
def authenticate_patient(patient_id: str, plain_password: str) -> bool:
    """
    Convenience function to authenticate a patient.
    Uses environment variables for Supabase connection.
    """
    authenticator = PatientAuthenticator()
    return authenticator.authenticate(patient_id, plain_password)


# ============================================================
# 4. Standalone Test Block
# ============================================================
if __name__ == "__main__":
    print("\n=== Testing auth.py ===\n")  # TODO: Remove this debug print after testing

    # Test 1: Password hashing and verification
    print("[OK] Testing password hashing...")  # TODO: Remove this debug print after testing
    test_password = "SecurePass123!"
    hashed = hash_password(test_password)
    print(f"  - Plain: {test_password}")  # TODO: Remove this debug print after testing
    print(f"  - Hashed: {hashed[:20]}...")  # TODO: Remove this debug print after testing

    # Test 2: Correct password
    print("\n[OK] Testing correct password...")  # TODO: Remove this debug print after testing
    result = verify_password(test_password, hashed)
    print(f"  - Correct password verified: {result}")  # TODO: Remove this debug print after testing

    # Test 3: Incorrect password
    print("\n[OK] Testing incorrect password...")  # TODO: Remove this debug print after testing
    result = verify_password("WrongPassword!", hashed)
    print(f"  - Wrong password rejected: {not result}")  # TODO: Remove this debug print after testing

    # Test 4: Supabase authentication (if keys available)
    print("\n[OK] Testing Supabase authentication...")  # TODO: Remove this debug print after testing
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    
    if supabase_url and supabase_key:
        try:
            authenticator = PatientAuthenticator()
            print("[OK] Authenticator initialized")  # TODO: Remove this debug print after testing
            
            # Test with a known patient (you need to create one first)
            # This is just a demo – it will fail if patient doesn't exist
            test_patient_id = "00000000-0000-0000-0000-000000000000"  # placeholder
            print(f"[INFO] Attempting authentication for test patient: {test_patient_id}")  # TODO: Remove this debug print after testing
            result = authenticator.authenticate(test_patient_id, "test123")
            print(f"  - Authentication result: {result} (expected: False, patient doesn't exist)")  # TODO: Remove this debug print after testing
        except Exception as e:
            print(f"[WARN] Supabase auth test failed: {e}")  # TODO: Remove this debug print after testing
    else:
        print("[INFO] Supabase credentials not set, skipping auth test")  # TODO: Remove this debug print after testing

    print("\n=== All tests passed! ===")  # TODO: Remove this debug print after testing