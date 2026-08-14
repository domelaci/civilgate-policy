// Canadian federal MP lookup via represent.opennorth.ca
export async function onRequestGet(context) {
  const raw = new URL(context.request.url).searchParams.get('postcode') || '';
  const postcode = raw.toUpperCase().replace(/\s+/g, '');

  if (!postcode || !/^[A-Z]\d[A-Z]\d[A-Z]\d$/.test(postcode)) {
    return json({ error: 'Enter a valid Canadian postal code (e.g. M5V3A8).' }, 400);
  }

  const url = `https://represent.opennorth.ca/postcodes/${encodeURIComponent(postcode)}/`;
  try {
    const r = await fetch(url, {
      headers: { 'User-Agent': 'CivilGate/1.0 (domelaci@gmail.com)' },
    });
    if (r.status === 404) {
      return json({ error: 'Postal code not recognised. Try without spaces (e.g. M5V3A8).' }, 404);
    }
    if (!r.ok) {
      return json({ error: 'Representative data unavailable. Try again later.' }, 502);
    }
    const data = await r.json();
    const reps = (data.representatives_centroid || [])
      .filter(m => (m.representative_set_name || '').includes('House of Commons'))
      .map(m => ({
        name: m.name,
        role: `MP — ${m.district_name || 'House of Commons'}`,
        party: m.party_name || '',
        email: m.email || '',
        phone: m.phone || '',
        url: m.url || '',
      }));
    return json({ reps });
  } catch {
    return json({ error: 'Could not load representative data.' }, 502);
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'public, max-age=86400',
    },
  });
}
