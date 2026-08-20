import { createClient } from '@supabase/supabase-js';
import crypto from 'crypto';
import 'dotenv/config';
const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);

async function uploadMedicalDocument(patientId, file, category) {
    const fileHash = crypto.createHash('sha256').update(file.buffer).digest('hex');
    const timestamp = Date.now();
    const storagePath = `${patientId}/${timestamp}_${file.originalname}`;

    const { data: uploadData, error: uploadError } = await supabase.storage
        .from('medical-documents')
        .upload(storagePath, file.buffer, {
            contentType: file.mimetype,
            upsert: false,
        });

    if (uploadError) throw new Error(`Storage upload failed: ${uploadError.message}`);

    const { data: docData, error: dbError } = await supabase
        .from('documents')
        .insert({
            patient_id: patientId,
            file_path: uploadData.path,
            file_size_bytes: file.size,
            mime_type: file.mimetype,
            original_filename: file.originalname,
            file_hash: fileHash,
            category: category,
        })
        .select()
        .single();

    if (dbError) {
        await supabase.storage.from('medical-documents').remove([storagePath]);
        throw new Error(`Database insert failed: ${dbError.message}`);
    }

    return docData;
}

async function getMedicalDocumentUrl(documentId) {
    const { data: doc, error: fetchError } = await supabase
        .from('documents')
        .select('file_path, file_hash, mime_type, original_filename')
        .eq('id', documentId)
        .single();

    if (fetchError || !doc) throw new Error('Document not found');

    const { data: signedData, error: signError } = await supabase.storage
        .from('medical-documents')
        .createSignedUrl(doc.file_path, 3600);

    if (signError) throw new Error(`Could not generate access URL: ${signError.message}`);

    return {
        url: signedData.signedUrl,
        filename: doc.original_filename,
        mimeType: doc.mime_type,
        expiresIn: 3600,
    };
}

async function saveExtractedData(documentId, extractedValues) {
    const rows = extractedValues.map(v => ({
        document_id: documentId,
        test_name: v.test_name,
        value: v.value,
        value_numeric: v.value_numeric ?? null,
        unit: v.unit ?? null,
        normal_range: v.normal_range ?? null,
        flag: v.flag ?? null,
    }));

    const { data, error } = await supabase
        .from('extracted_data')
        .insert(rows)
        .select();

    if (error) throw new Error(`Extracted data insert failed: ${error.message}`);
    return data;
}

async function saveSummaryEmbedding(patientId, documentId, summaryText) {
    const fakeEmbedding = Array.from({ length: 1536 }, () => Math.random());

    const { data, error } = await supabase
        .from('summaries_vectors')
        .insert({
            patient_id: patientId,
            source_id: documentId,
            source_type: 'ai_summary',
            content: summaryText,
            embedding: fakeEmbedding,
        })
        .select()
        .single();

    if (error) throw new Error(`Embedding insert failed: ${error.message}`);
    return data;
}

export { uploadMedicalDocument, getMedicalDocumentUrl, saveExtractedData, saveSummaryEmbedding };