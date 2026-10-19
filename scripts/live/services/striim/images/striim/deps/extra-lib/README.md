# Extra JDBC drivers

Put a JDBC driver jar here to add it to the framework's Striim image: the build copies every
`*.jar` in this folder into Striim's `lib/`. Adding, changing or removing a jar rebuilds the image
on the next provision; stop a kept cluster so it is re-created from the new image. Jars are not
committed. The driver's own licence applies.
