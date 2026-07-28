-- Run once in Supabase SQL Editor.
-- The bucket remains private. Only the backend service-role key reads/writes it.

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
    20000000,
    array[
        'audio/mpeg',
        'audio/wav',
        'audio/ogg',
        'audio/mp4',
        'audio/aac',
        'audio/webm',
        'audio/flac',
        'application/json'
    ]::text[]
)
on conflict (id) do update set
    name = excluded.name,
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;
