-- Run once in Supabase SQL Editor, and rerun after upgrading from an older
-- unencrypted audio extension. The bucket remains private. Only the trusted
-- backend secret/service-role key reads or writes it.
--
-- 20,065,936 bytes covers a 20,000,000-byte plaintext file plus the bounded
-- AES-256-GCM chunk container overhead configured in source_settings.py.

insert into storage.buckets (
    id,
    name,
    public,
    file_size_limit,
    allowed_mime_types
)
values (
    'website-audio',
    'website-audio',
    false,
    20065936,
    array[
        'application/octet-stream',
        'application/json',
        'audio/mpeg',
        'audio/wav',
        'audio/ogg',
        'audio/mp4',
        'audio/aac',
        'audio/webm',
        'audio/flac'
    ]::text[]
)
on conflict (id) do update set
    name = excluded.name,
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
