# Quick deletion script
from clients import SupabaseClient
client = SupabaseClient()
client.client.storage.from_('medical-documents').remove([
    'a9541a64-95bc-4238-8869-bdb035d2ac76/4cc23657153430e7c74e3d948a7ebd039083532cfcad7b5a7b8431f2ed5adc3b.png'
])
print("File deleted")