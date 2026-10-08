package com.example.common;

import com.webaction.runtime.compiler.custom.Nondeterministic;
import com.webaction.web.api.VaultAPI;

/**
 * Production {@link SecretResolver}: resolves {@code [namespace.vaultName.key]} vault references
 * via the platform {@link VaultAPI}, passing plain-text values through verbatim. Shared so every OP that opens a credentialed connection inherits vault support
 * from the shared layer instead of copy-pasting it.
 *
 * <p>The security-manager token comes from a lazily-resolving {@link SecurityTokenProvider}, and
 * {@link VaultAPI} is only constructed when a whole-value bracketed reference needs resolving — so
 * constructing this class, and resolving a non-reference value, never touches
 * {@code MDCache} or {@code VaultAPI}, and hermetic unit tests can exercise those paths freely.
 * Only an actual vault lookup reaches the live platform; behavioural coverage of that path comes
 * from live-tier tests, per the same policy as {@link MdrTypeResolver}.</p>
 */
public class VaultSecretResolver implements SecretResolver {

    private final Logger logger;
    private final SecurityTokenProvider tokens = new SecurityTokenProvider();

    /** Production constructor: no eager MDCache/VaultAPI access. */
    public VaultSecretResolver(Logger logger) {
        this.logger = logger;
    }

    /**
     * {@inheritDoc}
     *
     * <p>{@code @Nondeterministic} is carried over verbatim from the pre-extraction
     * {@code JdbcConnectionFactory.getVaultProperty}, where it was documented as telling the Striim
     * runtime optimizer not to memoize the result — a vault secret is resolved at runtime and may
     * change between calls.</p>
     *
     * <p><b>Caveat, recorded rather than glossed over.</b> The annotation is
     * {@code RetentionPolicy.RUNTIME}, so it is reflectively readable — but there is no evidence
     * the optimizer inspects it here, and two things argue it never did: this method is reached
     * only by a plain Java call from inside an OP ({@code JdbcConnectionFactory}), not through any
     * TQL-dispatched function, and in its previous home it sat on a {@code private} method, which
     * the optimizer could not have dispatched through either. The extraction therefore did not
     * regress anything — but if the annotation IS load-bearing somewhere, note it now sits on an
     * {@code @Override} reached through the {@link SecretResolver} interface, and a consumer that
     * resolves annotations from the declared interface method rather than the implementation would
     * not see it. Worth a check by someone who knows the optimizer's lookup rules.</p>
     */
    @Override
    @Nondeterministic
    public String resolve(final String value) {
        try {
            String[] ref = parseVaultRef(value);
            if (ref == null)
                return null; // whole-value reference but malformed -- parseVaultRef already logged
            if (ref.length == 1)
                return ref[0]; // not a vault reference -- pass through verbatim
            VaultAPI api = new VaultAPI();
            return api.getValue(tokens.token(), ref[0], ref[1]).value;
        } catch (Exception e) {
            // Resolution failed: log clearly. The caller proceeds with a null value, so a
            // subsequent DB connection will likely fail -- make the vault cause explicit here.
            logger.logError(() -> "Vault lookup failed for '" + value + "': " + e.getMessage()
                    + " — proceeding with no value (DB connection will likely fail)");
        }
        return null;
    }

    /**
     * Package-private test seam: parses a {@code Password} value as a possible vault reference,
     * without touching {@link VaultAPI} or the security-manager token — everything past this point
     * in {@link #resolve} is platform coupling this class exists specifically to isolate, so the
     * parsing itself is pulled out to stay unit-testable in isolation.
     *
     * <p>Returns a 1-element array {@code {value}} unless the whole value is one bracket group
     * ({@code [x]} or {@code [[x]]}, with no brackets inside). A well-formed reference returns
     * {@code {vaultId, key}}; a whole-value bracket group with fewer than 3 dot-separated parts
     * returns {@code null} after logging.</p>
     *
     * <p><b>A reference with MORE than 3 parts is accepted, not rejected.</b> Given
     * {@code [a.b.c.d]}, only the first two parts become {@code vaultId} and the third becomes
     * {@code key}; anything past the third is silently ignored. Pre-existing behaviour, kept.</p>
     *
     * <p>Deliberately not {@code static}: this method logs the malformed case itself through the
     * instance {@code logger}, which a static could not do without threading the logger through as
     * a parameter. And returning a 1-element array for "not a vault reference" — rather than
     * {@code null}, which is also the malformed case's return — lets the caller distinguish the two
     * without re-deriving the bracket check.</p>
     */
    String[] parseVaultRef(final String value) {
        String cleanName;
        if (value.startsWith("[[") && value.endsWith("]]")) {
            cleanName = value.substring(2, value.length() - 2);
        } else if (value.startsWith("[") && value.endsWith("]")) {
            cleanName = value.substring(1, value.length() - 1);
        } else {
            return new String[] { value };
        }
        if (cleanName.indexOf('[') >= 0 || cleanName.indexOf(']') >= 0)
            return new String[] { value };
        String[] parts = cleanName.split("\\.");
        if (parts.length < 3) {
            logger.logError(() -> "Malformed vault reference '" + value
                    + "' — expected [namespace.vaultName.key]; cannot resolve");
            return null;
        }
        String vaultId = String.format("%s.VAULT.%s", parts[0], parts[1]);
        return new String[] { vaultId, parts[2] };
    }
}
