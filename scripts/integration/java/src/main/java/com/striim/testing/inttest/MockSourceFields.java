package com.striim.testing.inttest;

import java.lang.reflect.Field;

/**
 * Supplies placeholder {@link Field} objects for the harness's mock {@code BuiltInFuncs.getFieldsArray}.
 * The real Striim {@code BuiltInFunc.getFieldsArray} returns the reflected fields of a generated
 * source-event class; {@code WAEventFactory} only reads each field's {@code getName()} and immediately
 * maps it through {@code getAliasFieldName} to the real source-column name — so these placeholders
 * (named {@code f0..f1599}) never need to match a real column name; they are positional handles the
 * companion alias map (also built by the harness) renames. Capacity {@value #CAPACITY} columns.
 */
final class MockSourceFields {

    /**
     * ⚠ RAISED FROM 64 TO 256 for T3's 200-column perf workload (§55.4 asked for a very wide
     * table; §69.3 is the case), then to 1600 for wide-table cases at each engine's column
     * limit (PostgreSQL's 1600 is the largest the cases reach). The cap is not a modelling choice — these are positional
     * placeholder handles an alias map renames, so the only cost of more is a wider class.
     * A schema beyond this still fails with the message that names this constant.
     */
    static final int CAPACITY = 1600;

    // Positional placeholder fields f0..f1599 (see class javadoc).
    public Object f0, f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13, f14, f15;
    public Object f16, f17, f18, f19, f20, f21, f22, f23, f24, f25, f26, f27, f28, f29, f30, f31;
    public Object f32, f33, f34, f35, f36, f37, f38, f39, f40, f41, f42, f43, f44, f45, f46, f47;
    public Object f48, f49, f50, f51, f52, f53, f54, f55, f56, f57, f58, f59, f60, f61, f62, f63;
    public Object f64, f65, f66, f67, f68, f69, f70, f71, f72, f73, f74, f75, f76, f77, f78, f79;
    public Object f80, f81, f82, f83, f84, f85, f86, f87, f88, f89, f90, f91, f92, f93, f94, f95;
    public Object f96, f97, f98, f99, f100, f101, f102, f103, f104, f105, f106, f107, f108, f109, f110, f111;
    public Object f112, f113, f114, f115, f116, f117, f118, f119, f120, f121, f122, f123, f124, f125, f126, f127;
    public Object f128, f129, f130, f131, f132, f133, f134, f135, f136, f137, f138, f139, f140, f141, f142, f143;
    public Object f144, f145, f146, f147, f148, f149, f150, f151, f152, f153, f154, f155, f156, f157, f158, f159;
    public Object f160, f161, f162, f163, f164, f165, f166, f167, f168, f169, f170, f171, f172, f173, f174, f175;
    public Object f176, f177, f178, f179, f180, f181, f182, f183, f184, f185, f186, f187, f188, f189, f190, f191;
    public Object f192, f193, f194, f195, f196, f197, f198, f199, f200, f201, f202, f203, f204, f205, f206, f207;
    public Object f208, f209, f210, f211, f212, f213, f214, f215, f216, f217, f218, f219, f220, f221, f222, f223;
    public Object f224, f225, f226, f227, f228, f229, f230, f231, f232, f233, f234, f235, f236, f237, f238, f239;
    public Object f240, f241, f242, f243, f244, f245, f246, f247, f248, f249, f250, f251, f252, f253, f254, f255;
    public Object f256, f257, f258, f259, f260, f261, f262, f263, f264, f265, f266, f267, f268, f269, f270, f271;
    public Object f272, f273, f274, f275, f276, f277, f278, f279, f280, f281, f282, f283, f284, f285, f286, f287;
    public Object f288, f289, f290, f291, f292, f293, f294, f295, f296, f297, f298, f299, f300, f301, f302, f303;
    public Object f304, f305, f306, f307, f308, f309, f310, f311, f312, f313, f314, f315, f316, f317, f318, f319;
    public Object f320, f321, f322, f323, f324, f325, f326, f327, f328, f329, f330, f331, f332, f333, f334, f335;
    public Object f336, f337, f338, f339, f340, f341, f342, f343, f344, f345, f346, f347, f348, f349, f350, f351;
    public Object f352, f353, f354, f355, f356, f357, f358, f359, f360, f361, f362, f363, f364, f365, f366, f367;
    public Object f368, f369, f370, f371, f372, f373, f374, f375, f376, f377, f378, f379, f380, f381, f382, f383;
    public Object f384, f385, f386, f387, f388, f389, f390, f391, f392, f393, f394, f395, f396, f397, f398, f399;
    public Object f400, f401, f402, f403, f404, f405, f406, f407, f408, f409, f410, f411, f412, f413, f414, f415;
    public Object f416, f417, f418, f419, f420, f421, f422, f423, f424, f425, f426, f427, f428, f429, f430, f431;
    public Object f432, f433, f434, f435, f436, f437, f438, f439, f440, f441, f442, f443, f444, f445, f446, f447;
    public Object f448, f449, f450, f451, f452, f453, f454, f455, f456, f457, f458, f459, f460, f461, f462, f463;
    public Object f464, f465, f466, f467, f468, f469, f470, f471, f472, f473, f474, f475, f476, f477, f478, f479;
    public Object f480, f481, f482, f483, f484, f485, f486, f487, f488, f489, f490, f491, f492, f493, f494, f495;
    public Object f496, f497, f498, f499, f500, f501, f502, f503, f504, f505, f506, f507, f508, f509, f510, f511;
    public Object f512, f513, f514, f515, f516, f517, f518, f519, f520, f521, f522, f523, f524, f525, f526, f527;
    public Object f528, f529, f530, f531, f532, f533, f534, f535, f536, f537, f538, f539, f540, f541, f542, f543;
    public Object f544, f545, f546, f547, f548, f549, f550, f551, f552, f553, f554, f555, f556, f557, f558, f559;
    public Object f560, f561, f562, f563, f564, f565, f566, f567, f568, f569, f570, f571, f572, f573, f574, f575;
    public Object f576, f577, f578, f579, f580, f581, f582, f583, f584, f585, f586, f587, f588, f589, f590, f591;
    public Object f592, f593, f594, f595, f596, f597, f598, f599, f600, f601, f602, f603, f604, f605, f606, f607;
    public Object f608, f609, f610, f611, f612, f613, f614, f615, f616, f617, f618, f619, f620, f621, f622, f623;
    public Object f624, f625, f626, f627, f628, f629, f630, f631, f632, f633, f634, f635, f636, f637, f638, f639;
    public Object f640, f641, f642, f643, f644, f645, f646, f647, f648, f649, f650, f651, f652, f653, f654, f655;
    public Object f656, f657, f658, f659, f660, f661, f662, f663, f664, f665, f666, f667, f668, f669, f670, f671;
    public Object f672, f673, f674, f675, f676, f677, f678, f679, f680, f681, f682, f683, f684, f685, f686, f687;
    public Object f688, f689, f690, f691, f692, f693, f694, f695, f696, f697, f698, f699, f700, f701, f702, f703;
    public Object f704, f705, f706, f707, f708, f709, f710, f711, f712, f713, f714, f715, f716, f717, f718, f719;
    public Object f720, f721, f722, f723, f724, f725, f726, f727, f728, f729, f730, f731, f732, f733, f734, f735;
    public Object f736, f737, f738, f739, f740, f741, f742, f743, f744, f745, f746, f747, f748, f749, f750, f751;
    public Object f752, f753, f754, f755, f756, f757, f758, f759, f760, f761, f762, f763, f764, f765, f766, f767;
    public Object f768, f769, f770, f771, f772, f773, f774, f775, f776, f777, f778, f779, f780, f781, f782, f783;
    public Object f784, f785, f786, f787, f788, f789, f790, f791, f792, f793, f794, f795, f796, f797, f798, f799;
    public Object f800, f801, f802, f803, f804, f805, f806, f807, f808, f809, f810, f811, f812, f813, f814, f815;
    public Object f816, f817, f818, f819, f820, f821, f822, f823, f824, f825, f826, f827, f828, f829, f830, f831;
    public Object f832, f833, f834, f835, f836, f837, f838, f839, f840, f841, f842, f843, f844, f845, f846, f847;
    public Object f848, f849, f850, f851, f852, f853, f854, f855, f856, f857, f858, f859, f860, f861, f862, f863;
    public Object f864, f865, f866, f867, f868, f869, f870, f871, f872, f873, f874, f875, f876, f877, f878, f879;
    public Object f880, f881, f882, f883, f884, f885, f886, f887, f888, f889, f890, f891, f892, f893, f894, f895;
    public Object f896, f897, f898, f899, f900, f901, f902, f903, f904, f905, f906, f907, f908, f909, f910, f911;
    public Object f912, f913, f914, f915, f916, f917, f918, f919, f920, f921, f922, f923, f924, f925, f926, f927;
    public Object f928, f929, f930, f931, f932, f933, f934, f935, f936, f937, f938, f939, f940, f941, f942, f943;
    public Object f944, f945, f946, f947, f948, f949, f950, f951, f952, f953, f954, f955, f956, f957, f958, f959;
    public Object f960, f961, f962, f963, f964, f965, f966, f967, f968, f969, f970, f971, f972, f973, f974, f975;
    public Object f976, f977, f978, f979, f980, f981, f982, f983, f984, f985, f986, f987, f988, f989, f990, f991;
    public Object f992, f993, f994, f995, f996, f997, f998, f999, f1000, f1001, f1002, f1003, f1004, f1005, f1006, f1007;
    public Object f1008, f1009, f1010, f1011, f1012, f1013, f1014, f1015, f1016, f1017, f1018, f1019, f1020, f1021, f1022, f1023;
    public Object f1024, f1025, f1026, f1027, f1028, f1029, f1030, f1031, f1032, f1033, f1034, f1035, f1036, f1037, f1038, f1039;
    public Object f1040, f1041, f1042, f1043, f1044, f1045, f1046, f1047, f1048, f1049, f1050, f1051, f1052, f1053, f1054, f1055;
    public Object f1056, f1057, f1058, f1059, f1060, f1061, f1062, f1063, f1064, f1065, f1066, f1067, f1068, f1069, f1070, f1071;
    public Object f1072, f1073, f1074, f1075, f1076, f1077, f1078, f1079, f1080, f1081, f1082, f1083, f1084, f1085, f1086, f1087;
    public Object f1088, f1089, f1090, f1091, f1092, f1093, f1094, f1095, f1096, f1097, f1098, f1099, f1100, f1101, f1102, f1103;
    public Object f1104, f1105, f1106, f1107, f1108, f1109, f1110, f1111, f1112, f1113, f1114, f1115, f1116, f1117, f1118, f1119;
    public Object f1120, f1121, f1122, f1123, f1124, f1125, f1126, f1127, f1128, f1129, f1130, f1131, f1132, f1133, f1134, f1135;
    public Object f1136, f1137, f1138, f1139, f1140, f1141, f1142, f1143, f1144, f1145, f1146, f1147, f1148, f1149, f1150, f1151;
    public Object f1152, f1153, f1154, f1155, f1156, f1157, f1158, f1159, f1160, f1161, f1162, f1163, f1164, f1165, f1166, f1167;
    public Object f1168, f1169, f1170, f1171, f1172, f1173, f1174, f1175, f1176, f1177, f1178, f1179, f1180, f1181, f1182, f1183;
    public Object f1184, f1185, f1186, f1187, f1188, f1189, f1190, f1191, f1192, f1193, f1194, f1195, f1196, f1197, f1198, f1199;
    public Object f1200, f1201, f1202, f1203, f1204, f1205, f1206, f1207, f1208, f1209, f1210, f1211, f1212, f1213, f1214, f1215;
    public Object f1216, f1217, f1218, f1219, f1220, f1221, f1222, f1223, f1224, f1225, f1226, f1227, f1228, f1229, f1230, f1231;
    public Object f1232, f1233, f1234, f1235, f1236, f1237, f1238, f1239, f1240, f1241, f1242, f1243, f1244, f1245, f1246, f1247;
    public Object f1248, f1249, f1250, f1251, f1252, f1253, f1254, f1255, f1256, f1257, f1258, f1259, f1260, f1261, f1262, f1263;
    public Object f1264, f1265, f1266, f1267, f1268, f1269, f1270, f1271, f1272, f1273, f1274, f1275, f1276, f1277, f1278, f1279;
    public Object f1280, f1281, f1282, f1283, f1284, f1285, f1286, f1287, f1288, f1289, f1290, f1291, f1292, f1293, f1294, f1295;
    public Object f1296, f1297, f1298, f1299, f1300, f1301, f1302, f1303, f1304, f1305, f1306, f1307, f1308, f1309, f1310, f1311;
    public Object f1312, f1313, f1314, f1315, f1316, f1317, f1318, f1319, f1320, f1321, f1322, f1323, f1324, f1325, f1326, f1327;
    public Object f1328, f1329, f1330, f1331, f1332, f1333, f1334, f1335, f1336, f1337, f1338, f1339, f1340, f1341, f1342, f1343;
    public Object f1344, f1345, f1346, f1347, f1348, f1349, f1350, f1351, f1352, f1353, f1354, f1355, f1356, f1357, f1358, f1359;
    public Object f1360, f1361, f1362, f1363, f1364, f1365, f1366, f1367, f1368, f1369, f1370, f1371, f1372, f1373, f1374, f1375;
    public Object f1376, f1377, f1378, f1379, f1380, f1381, f1382, f1383, f1384, f1385, f1386, f1387, f1388, f1389, f1390, f1391;
    public Object f1392, f1393, f1394, f1395, f1396, f1397, f1398, f1399, f1400, f1401, f1402, f1403, f1404, f1405, f1406, f1407;
    public Object f1408, f1409, f1410, f1411, f1412, f1413, f1414, f1415, f1416, f1417, f1418, f1419, f1420, f1421, f1422, f1423;
    public Object f1424, f1425, f1426, f1427, f1428, f1429, f1430, f1431, f1432, f1433, f1434, f1435, f1436, f1437, f1438, f1439;
    public Object f1440, f1441, f1442, f1443, f1444, f1445, f1446, f1447, f1448, f1449, f1450, f1451, f1452, f1453, f1454, f1455;
    public Object f1456, f1457, f1458, f1459, f1460, f1461, f1462, f1463, f1464, f1465, f1466, f1467, f1468, f1469, f1470, f1471;
    public Object f1472, f1473, f1474, f1475, f1476, f1477, f1478, f1479, f1480, f1481, f1482, f1483, f1484, f1485, f1486, f1487;
    public Object f1488, f1489, f1490, f1491, f1492, f1493, f1494, f1495, f1496, f1497, f1498, f1499, f1500, f1501, f1502, f1503;
    public Object f1504, f1505, f1506, f1507, f1508, f1509, f1510, f1511, f1512, f1513, f1514, f1515, f1516, f1517, f1518, f1519;
    public Object f1520, f1521, f1522, f1523, f1524, f1525, f1526, f1527, f1528, f1529, f1530, f1531, f1532, f1533, f1534, f1535;
    public Object f1536, f1537, f1538, f1539, f1540, f1541, f1542, f1543, f1544, f1545, f1546, f1547, f1548, f1549, f1550, f1551;
    public Object f1552, f1553, f1554, f1555, f1556, f1557, f1558, f1559, f1560, f1561, f1562, f1563, f1564, f1565, f1566, f1567;
    public Object f1568, f1569, f1570, f1571, f1572, f1573, f1574, f1575, f1576, f1577, f1578, f1579, f1580, f1581, f1582, f1583;
    public Object f1584, f1585, f1586, f1587, f1588, f1589, f1590, f1591, f1592, f1593, f1594, f1595, f1596, f1597, f1598, f1599;

    private MockSourceFields() {
    }

    /**
     * One placeholder {@link Field} per declared column, positional.
     *
     * <p>Shared by the OP path's injected {@code BuiltInFuncs} proxy and the target path's static
     * {@code BuiltInFunc}, so a case's {@code types:} block resolves to the same field array
     * whichever driver reads it. Two hand-written copies of this pairing is how the two would
     * disagree about a schema nobody changed.</p>
     */
    static Field[] fieldsFor(java.util.List<String> columns) {
        if (columns == null) {
            return new Field[0];
        }
        Field[] fields = new Field[columns.size()];
        for (int i = 0; i < fields.length; i++) {
            fields[i] = field(i);
        }
        return fields;
    }

    /** The companion alias map the placeholders are renamed through: {@code f<i> -> column}. */
    static java.util.Map<String, String> aliasesFor(java.util.List<String> columns) {
        java.util.Map<String, String> aliases = new java.util.HashMap<>();
        if (columns != null) {
            for (int i = 0; i < columns.size(); i++) {
                aliases.put("f" + i, columns.get(i));
            }
        }
        return aliases;
    }

    /** The placeholder field named {@code "f"+index}; throws if {@code index >= }{@value #CAPACITY}. */
    static Field field(int index) {
        if (index >= CAPACITY) {
            throw new IllegalArgumentException(
                    "types: source schema has more than " + CAPACITY + " columns (index " + index
                            + "); raise MockSourceFields.CAPACITY if a real fixture needs it");
        }
        try {
            return MockSourceFields.class.getDeclaredField("f" + index);
        } catch (NoSuchFieldException e) {
            throw new IllegalStateException("missing placeholder field f" + index, e);
        }
    }

    // ---- created types: fields named by column ----

    /** Field names of each type the mock TypeResolver created, by its UUID; one case per JVM. */
    private static final java.util.Map<Object, java.util.List<String>> CREATED =
            new java.util.concurrent.ConcurrentHashMap<>();
    private static final java.util.Map<java.util.List<String>, Field[]> NAMED =
            new java.util.concurrent.ConcurrentHashMap<>();

    /** Records a created type's field names, in declaration order, so its field array can be served. */
    static void registerCreated(Object uuid, java.util.List<String> fieldNames) {
        if (uuid != null && fieldNames != null) {
            CREATED.put(uuid, java.util.List.copyOf(fieldNames));
        }
    }

    /** A created type's field names, or null for a type the mock did not create. */
    static java.util.List<String> createdFields(Object uuid) {
        return uuid == null ? null : CREATED.get(uuid);
    }

    /**
     * Real {@link Field}s named by {@code names}, in that order -- what a platform-generated type
     * class reflects, and what a core that proves a created type's field order compares against.
     * Compiled once per name list from a generated class with one public field per name. Returns
     * null when that is impossible here (no JDK compiler, or a name that is not a Java
     * identifier), so the caller falls back to what it served before.
     */
    static Field[] namedFields(java.util.List<String> names) {
        if (names == null || names.isEmpty()) {
            return null;
        }
        for (String n : names) {
            if (!javax.lang.model.SourceVersion.isName(n)) {
                return null;
            }
        }
        return NAMED.computeIfAbsent(java.util.List.copyOf(names), MockSourceFields::compileNamed);
    }

    private static Field[] compileNamed(java.util.List<String> names) {
        javax.tools.JavaCompiler compiler = javax.tools.ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            return null;
        }
        try {
            java.nio.file.Path dir = java.nio.file.Files.createTempDirectory("inttest-type-");
            String cls = "CreatedType" + Integer.toHexString(names.hashCode());
            StringBuilder src = new StringBuilder("public class ").append(cls).append(" {\n");
            for (String n : names) {
                src.append("    public Object ").append(n).append(";\n");
            }
            src.append("}\n");
            java.nio.file.Path file = dir.resolve(cls + ".java");
            java.nio.file.Files.writeString(file, src);
            if (compiler.run(null, null, null, "-d", dir.toString(), file.toString()) != 0) {
                return null;
            }
            Class<?> type = new java.net.URLClassLoader(new java.net.URL[] { dir.toUri().toURL() },
                    MockSourceFields.class.getClassLoader()).loadClass(cls);
            Field[] fields = new Field[names.size()];
            for (int i = 0; i < fields.length; i++) {
                fields[i] = type.getField(names.get(i));
            }
            return fields;
        } catch (Exception e) {
            return null;
        }
    }
}
