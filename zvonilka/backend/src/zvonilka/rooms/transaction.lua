-- One bounded standalone Redis aggregate; all keys passed explicitly.
-- No mutation until validation and serialization complete.
local key = KEYS[1]
local request = cjson.decode(ARGV[1])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local raw = redis.call('GET', key)
local data = raw and cjson.decode(raw) or {schema=1, rooms={}}
if data.schema ~= 1 then return cjson.encode({ok=false, code='schema_mismatch'}) end
local cfg = request.limits
local rooms = data.rooms
local function count(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end
local function fail(code)
    return cjson.encode({ok=false, code=code})
end
local function close(r)
    r.state = 'closed'
    r.closed_at = now
    r.empty_at = nil
    r.present = {}
end
-- Leases are per user and per connection generation. Expiry starts grace
-- at the expiry instant, not at the later sweep time.
for id, r in pairs(rooms) do
    local changed = false
    local last_expiry = nil
    for user, p in pairs(r.present) do
        if p.expires_at <= now then
            last_expiry = math.max(last_expiry or p.expires_at, p.expires_at)
            r.present[user] = nil
            changed = true
        end
    end
    if r.state == 'active' and count(r.present) == 0 then
        r.state = 'recovering'
        r.empty_at = last_expiry or now
        changed = true
    end
    if r.state ~= 'closed' then
        if (r.state == 'created' and r.pending_until <= now)
            or (r.activated_at and r.deadline <= now)
            or (r.state == 'recovering' and r.empty_at + r.grace <= now) then
            close(r)
            changed = true
        end
    end
    if changed then r.revision = r.revision + 1 end
    if r.state == 'closed' and r.closed_at + r.retention <= now then
        rooms[id] = nil
    end
end
local function snapshot(r)
    local members = {}
    for u in pairs(r.admitted) do
        members[#members+1] = {
            user_id = u,
            online = r.present[u] ~= nil
        }
    end

    table.sort(members, function(a, b)
        return a.user_id < b.user_id
    end)

    return {
        room_id = request.room_id,
        owner_id = r.owner,
        state = r.state,
        revision = r.revision,
        activated_at = r.activated_at,
        deadline = r.deadline,
        empty_at = r.empty_at,
        members = members
    }
end
local result = {}
local op = request.op
local r = rooms[request.room_id or '']
if op == 'create' then
    if r then return fail('room_id_exists') end
    if count(rooms) >= cfg.max_records then return fail('registry_full') end
    local pending = 0
    for _, other in pairs(rooms) do
        if other.owner == request.actor and other.state == 'created' then
            pending = pending + 1
        end
    end
    if pending >= cfg.pending_per_owner then return fail('pending_limit') end
    r = {owner=request.actor, state='created', created_at=now,
        pending_until=now+cfg.pending_ttl, grace=cfg.grace,
        lifetime=cfg.lifetime, retention=cfg.closed_retention,
        admitted={}, present={}, invites={}, revision=1}
    r.admitted[request.actor] = true
    rooms[request.room_id] = r
    result = snapshot(r)
elseif op == 'sweep' then
    result = {records=count(rooms)}
else
    if not r then return fail('room_not_found') end
    if op == 'snapshot' then
        if not r.admitted[request.actor] then return fail('not_admitted') end
        result = snapshot(r)
    elseif op == 'close' then
        if r.owner ~= request.actor then return fail('not_owner') end
        if r.state ~= 'closed' then
            close(r)
            r.revision = r.revision + 1
        end
        result = snapshot(r)
    elseif r.state == 'closed' then
        return fail('room_closed')
    elseif op == 'issue_invite' then
        if r.owner ~= request.actor then return fail('not_owner') end
        if r.invites[request.digest] then return fail('invite_exists') end
        local outstanding = 0
        for _, inv in pairs(r.invites) do
            if not inv.used_by and not inv.revoked and inv.expires_at > now then
                outstanding = outstanding + 1
            end
        end
        if count(r.invites) >= cfg.invites_per_room then return fail('invite_history_limit') end
        if outstanding >= cfg.participants - count(r.admitted) then
            return fail('invite_limit')
        end
        r.invites[request.digest] = {expires_at=now+cfg.invite_ttl}
        r.revision = r.revision + 1
        result = {expires_at=r.invites[request.digest].expires_at}
    elseif op == 'revoke_invite' then
        if r.owner ~= request.actor then return fail('not_owner') end
        local inv = r.invites[request.digest]
        if not inv then return fail('invite_not_found') end
        if inv.used_by then return fail('invite_consumed') end
        if not inv.revoked then
            inv.revoked = true
            r.revision = r.revision + 1
        end
        result = {}
    elseif op == 'redeem' then
        local inv = r.invites[request.digest]
        if not inv then return fail('invite_not_found') end
        -- Lost-response retry: consumed token remains bound to the same account.
        if inv.used_by then
            if inv.used_by ~= request.actor then return fail('invite_consumed') end
            result = snapshot(r)
        else
            if inv.revoked then return fail('invite_revoked') end
            if inv.expires_at <= now then return fail('invite_expired') end
            if not r.admitted[request.actor] and count(r.admitted) >= cfg.participants then
                return fail('participant_limit')
            end
            r.admitted[request.actor] = true
            inv.used_by = request.actor
            inv.used_at = now
            r.revision = r.revision + 1
            result = snapshot(r)
        end
    elseif op == 'join' then
        if not r.admitted[request.actor] then return fail('not_admitted') end
        if r.state == 'created' then
            if request.overloaded then return fail('overloaded') end
            local occupied = 0
            local owned = 0
            for _, other in pairs(rooms) do
                if other.state == 'active' or other.state == 'recovering' then
                    occupied = occupied + 1
                    if other.owner == r.owner then owned = owned + 1 end
                end
            end
            if occupied >= cfg.rooms then return fail('room_limit') end
            if owned >= cfg.owner_rooms then return fail('owner_room_limit') end
            r.activated_at = now
            r.deadline = now + r.lifetime
        end
        local old = r.present[request.actor]
        -- generation is server-created; identical join is safe to retry.
        result = {}
        if old and old.generation ~= request.generation then
            result.replaced_generation = old.generation
        end
        r.present[request.actor] = {generation=request.generation,
                                  expires_at=now+cfg.presence_ttl}
        r.state = 'active'
        r.empty_at = nil
        r.revision = r.revision + 1
        result.room = snapshot(r)
    elseif op == 'heartbeat' or op == 'leave' then
        if not r.admitted[request.actor] then return fail('not_admitted') end
        local p = r.present[request.actor]
        if not p or p.generation ~= request.generation then
            return fail('stale_generation')
        end
        if op == 'heartbeat' then
            p.expires_at = now + cfg.presence_ttl
        else
            r.present[request.actor] = nil
            if count(r.present) == 0 then
                r.state = 'recovering'
                r.empty_at = now
            end
            r.revision = r.revision + 1
        end
        result = snapshot(r)
    else
        return fail('unknown_operation')
    end
end
-- The final SET is the only write: Redis script runtime errors cannot cause
-- partial multi-key writes. Memory usage and execution time must be measured.
local serialized = cjson.encode(data)
redis.call('SET', key, serialized)
return cjson.encode({ok=true, value=result})
