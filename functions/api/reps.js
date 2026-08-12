export async function onRequestGet(context) {
  const { GOOGLE_CIVIC_KEY } = context.env;
  const address = new URL(context.request.url).searchParams.get('address');

  if (!address) {
    return new Response(JSON.stringify({ error: 'address required' }), {
      status: 400, headers: { 'Content-Type': 'application/json' },
    });
  }

  if (!GOOGLE_CIVIC_KEY) {
    return new Response(JSON.stringify({ error: 'Civic API key not configured' }), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    });
  }

  const url = `https://civicinfo.googleapis.com/civicinfo/v2/representatives?address=${encodeURIComponent(address)}&key=${GOOGLE_CIVIC_KEY}`;

  try {
    const r    = await fetch(url);
    const data = await r.json();
    return new Response(JSON.stringify(data), {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'upstream fetch failed' }), {
      status: 502, headers: { 'Content-Type': 'application/json' },
    });
  }
}
