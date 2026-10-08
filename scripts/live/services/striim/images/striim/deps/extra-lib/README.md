# Extra JDBC drivers

Put a JDBC driver jar here to add it to the framework's Striim image: the build copies every
`*.jar` in this folder into Striim's `lib/`. The image is not rebuilt when this folder changes:
after adding or changing a jar, remove the image (`docker rmi <prefix>-slt-striim:<version>`) so
the next run builds it. Jars are not committed. The driver's own licence applies.
