export async function onRequestGet(context) {
  const postcode = new URL(context.request.url).searchParams.get('postcode')?.trim();

  if (!postcode) {
    return json({ error: 'postcode required' }, 400);
  }

  try {
    // Step 1: resolve postcode → constituency
    const searchR = await fetch(
      `https://members-api.parliament.uk/api/Location/Constituency/Search?searchText=${encodeURIComponent(postcode)}&skip=0&take=1`,
      { headers: { Accept: 'application/json', 'User-Agent': 'CivilGate/1.0' } }
    );
    const searchData = await searchR.json();
    const constituency = searchData.items?.[0]?.value;

    if (!constituency) {
      return json({ error: 'No constituency found for that postcode. Try a full UK postcode (e.g. SW1A 1AA).' }, 404);
    }

    const constituencyId   = constituency.id;
    const constituencyName = constituency.name;

    // Step 2: get current MP for this constituency
    const mpR = await fetch(
      `https://members-api.parliament.uk/api/Members/Search?IsCurrentMember=true&House=Commons&constituency=${constituencyId}&skip=0&take=1`,
      { headers: { Accept: 'application/json', 'User-Agent': 'CivilGate/1.0' } }
    );
    const mpData = await mpR.json();
    const mp = mpData.items?.[0]?.value;

    if (!mp) {
      return json({ reps: [{ name: 'MP not found', role: `${constituencyName} constituency`, email: '', phone: '', url: '' }] });
    }

    const profileUrl = `https://members.parliament.uk/member/${mp.id}/contact`;

    return json({
      reps: [{
        name:  mp.nameDisplayAs,
        role:  `MP for ${constituencyName}`,
        email: '',
        phone: '',
        url:   profileUrl,
      }],
    });
  } catch (e) {
    return json({ error: 'Failed to reach UK Parliament API. Try again later.' }, 502);
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
  });
}
