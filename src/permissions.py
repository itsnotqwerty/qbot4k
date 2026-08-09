VIEW_CHANNEL = 1 << 10  # 1024

def _everyone_cannot_view(
	channel: dict[str, object],
	guild_id: str,
) -> bool:
	overwrites = channel.get("permission_overwrites", [])

	if not isinstance(overwrites, list):
		return False

	for overwrite in overwrites:
		if not isinstance(overwrite, dict):
			continue

		is_everyone_role = (
			str(overwrite.get("id") or "") == guild_id
			and int(overwrite.get("type", -1)) == 0
		)

		if not is_everyone_role:
			continue

		try:
			denied_permissions = int(str(overwrite.get("deny") or "0"))
		except ValueError:
			return False

		return bool(denied_permissions & VIEW_CHANNEL)

	return False
