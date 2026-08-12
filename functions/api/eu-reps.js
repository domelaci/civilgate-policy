const COUNTRY_QID = {
  AT: 'Q40', BE: 'Q31', BG: 'Q219', CY: 'Q229', CZ: 'Q213',
  DE: 'Q183', DK: 'Q35', EE: 'Q191', ES: 'Q29', FI: 'Q33',
  FR: 'Q142', GR: 'Q41', HR: 'Q224', HU: 'Q28', IE: 'Q27',
  IT: 'Q38', LT: 'Q37', LU: 'Q32', LV: 'Q211', MT: 'Q233',
  NL: 'Q55', PL: 'Q36', PT: 'Q45', RO: 'Q218', SE: 'Q34',
  SI: 'Q215', SK: 'Q214',
};

const SPARQL = (qid) => `
SELECT DISTINCT ?mepLabel ?email WHERE {
  ?mep p:P39 ?stmt.
  ?stmt ps:P39 wd:Q27169.
  ?stmt pq:P580 ?start.
  ?mep wdt:P27 wd:${qid}.
  FILTER(?start >= "2024-01-01"^^xsd:dateTime)
  FILTER NOT EXISTS { ?stmt pq:P582 ?end. }
  OPTIONAL { ?mep wdt:P968 ?email. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT 30
`;

export async function onRequestGet(context) {
  const country = new URL(context.request.url).searchParams.get('country')?.toUpperCase();

  if (!country) {
    return json({ error: 'country param required (ISO 2-letter code)' }, 400);
  }

  const qid = COUNTRY_QID[country];
  if (!qid) {
    return json({ error: `EU country code not recognised: ${country}` }, 400);
  }

  const url = `https://query.wikidata.org/sparql?query=${encodeURIComponent(SPARQL(qid))}&format=json`;

  try {
    const r = await fetch(url, {
      headers: { 'User-Agent': 'CivilGate/1.0 (domelaci@gmail.com)' },
    });

    if (!r.ok) {
      return json({ error: `Wikidata returned ${r.status}` }, 502);
    }

    const data = await r.json();
    const meps = data.results.bindings.map(b => ({
      name: b.mepLabel?.value ?? '',
      email: (b.email?.value ?? '').replace('mailto:', ''),
    })).filter(m => m.name);

    return json({ country, meps });
  } catch (e) {
    return json({ error: 'upstream fetch failed' }, 502);
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
  });
}
