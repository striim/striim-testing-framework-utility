package com.example.common;

import com.webaction.runtime.compiler.Compiler;
import com.webaction.runtime.compiler.stmts.Stmt;
import com.webaction.uuid.UUID;

/**
 * Captures the UUID of a compiled {@code CreateTypeStmt} for {@link MdrTypeResolver#createType}.
 * Kept as a <b>top-level</b> class here, not a nested, anonymous or lambda one, because this module's sources
 * shade into every OP jar and framework-interface implementers must be top-level under the OP split
 * classloader.
 */
public class BuiltInFuncTypeCompileCallback implements Compiler.ExecutionCallback {

    private final Stmt stmt;
    public UUID uuid;

    public BuiltInFuncTypeCompileCallback(Stmt stmt) {
        this.stmt = stmt;
    }

    @Override
    public void execute(Stmt ignoredPlatformStmt, Compiler compiler) throws Exception {
        // Use the stmt captured at construction, not the platform-injected one -- see the
        // striim-api rules in the platform classloader contract.
        this.uuid = (UUID) compiler.compileStmt(this.stmt);
    }
}
