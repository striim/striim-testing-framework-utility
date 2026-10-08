package com.example.common;

import com.webaction.metaRepository.MDCache;
import com.webaction.uuid.AuthToken;

/**
 * The one shared home of the lazy security-manager-token idiom:
 * {@code MDCache.getInstance().getWASecurityManagerToken()}, resolved and cached on first
 * {@link #token()} call — never at construction. Extracted verbatim from
 * {@code MdrTypeResolver.token()}, which now delegates here, so platform-coupled helpers stop
 * copy-pasting the double-checked lazy block. Its holders in this module are
 * {@link MdrTypeResolver}, {@link MdrMetadataAccess} and {@link VaultSecretResolver}; reader
 * modules hold one directly too.
 *
 * <p>Laziness is load-bearing, not a style choice: the platform's {@code MDCache} is not guaranteed
 * initialized at OP construction time, and hermetic unit tests must be able to construct any class
 * holding one of these without a live Striim server. Constructing a {@code SecurityTokenProvider}
 * touches no platform state.</p>
 *
 * <p>A {@code null} token — the platform not yet initialized when first asked — is written to the
 * field like any other value, but that is indistinguishable from leaving it unset: the next
 * {@link #token()} call sees {@code null} and retries the lookup. So the effective behaviour is
 * "a null result is not cached", matching the original per-OP idiom — but note the retry comes from
 * {@code null} being the uninitialized sentinel, not from any explicit don't-cache branch. If
 * {@code AuthToken} ever gains a meaningful null-object representation, this needs revisiting.</p>
 */
public final class SecurityTokenProvider {

    private volatile AuthToken token;

    /** Lazily resolves and caches the security-manager token on first use. */
    public AuthToken token() {
        AuthToken t = token;
        if (t == null) {
            synchronized (this) {
                t = token;
                if (t == null) {
                    t = MDCache.getInstance().getWASecurityManagerToken();
                    token = t;
                }
            }
        }
        return t;
    }
}
