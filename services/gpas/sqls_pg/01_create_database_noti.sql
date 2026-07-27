-- PostgreSQL port of the notification-service schema.
--
-- MySQL used a separate `notification_service` database; here it is a schema
-- inside the single "gpas" Postgres database (NotificationDS connects with
-- ?currentSchema=notification_service, see
-- services/gpas/jboss_pg/configure_wildfly_noti_service-2025.2.0.cli),
-- authenticating as the same role as everything else (gpas_user).
--
-- Table shapes are copied verbatim from gPAS 2025.2.0's own
-- EclipseLink-generated DDL (captured live), not translated from the MySQL
-- dump - EclipseLink's target-database platform for this persistence unit is
-- hardcoded to MySQL inside the deployed EAR regardless of the JDBC driver
-- actually in use, so letting it auto-create against Postgres fails outright
-- (AUTO_INCREMENT/DATETIME/longtext are not valid Postgres syntax). This file
-- exists so its schema-generation probe finds the tables already present and
-- never attempts that DDL at all.

CREATE SCHEMA IF NOT EXISTS notification_service;
SET search_path TO notification_service;

CREATE TABLE IF NOT EXISTS configuration (
    id BIGINT NOT NULL,
    activated BOOLEAN,
    configkey VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    value TEXT,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGINT NOT NULL,
    client_id VARCHAR(255) NOT NULL,
    consumer_id VARCHAR(255) NOT NULL,
    creationdate TIMESTAMP NOT NULL,
    data TEXT,
    error_message TEXT,
    send_date TIMESTAMP NOT NULL,
    status VARCHAR(255),
    type VARCHAR(255) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS sequence (
    seq_name VARCHAR(50) NOT NULL,
    seq_count DECIMAL(38),
    PRIMARY KEY (seq_name)
);

INSERT INTO sequence (seq_name, seq_count) VALUES ('SEQ_GEN', 0)
    ON CONFLICT (seq_name) DO NOTHING;

INSERT INTO configuration (id, activated, configkey, description, value) VALUES (
    1,
    true,
    'notification.config',
    NULL,
    E'<org.emau.icmvc.ttp.notification.service.model.config.NotificationConfig>\r\n    <consumerConfigs>\r\n        <param>\r\n            <!-- Eindeutige ID des Consumers -->\r\n            <key>Dispatcher</key>\r\n            <value class="org.emau.icmvc.ttp.notification.service.model.config.ConsumerConfig">\r\n                <!-- EJB, HTTP oder MQTT -->\r\n                <connectionType>EJB</connectionType>\r\n                <!-- Alle Message-Typen, die der Consumer erhalten soll (oder \'*\' als Platzhalter fuer alle) -->\r\n                <messageTypes>\r\n                    <string>EPIX.AssignIdentity</string>\r\n                </messageTypes>\r\n                <!-- Alle Client-IDs von denen keine Notifikationen empfangen werden sollen -->\r\n                <excludeClientIdFilter class="set">\r\n                    <string>gICS_Web</string>\r\n                </excludeClientIdFilter>\r\n                <!-- Type-Spezifische Parameter als Key-/Value-Paare, hier Beispiel EJB -->\r\n                <parameter>\r\n                    <param>\r\n                        <key>ejb.jndi.url</key>\r\n                        <value>java:global/test-dispatcher-ear-1.15.3/dispatcher-services-1.15.3/InternalNotificationConsumer</value>\r\n                    </param>\r\n                </parameter>\r\n            </value>\r\n        </param>\r\n        <param>\r\n            <key>HTTPConsumer</key>\r\n            <value class="org.emau.icmvc.ttp.notification.service.model.config.ConsumerConfig">\r\n                <connectionType>HTTP</connectionType>\r\n                <messageTypes>\r\n                    <string>GICS.exampleMethodWithNotification</string>\r\n                </messageTypes>\r\n                <excludeClientIdFilter class="set">\r\n                    <string>E-PIX_Web</string>\r\n                </excludeClientIdFilter>\r\n                <parameter>\r\n                    <param>\r\n                        <key>url</key>\r\n                        <value>https://httpbin.org/post</value>\r\n                    </param>\r\n                    <param>\r\n                        <key>username</key>\r\n                        <value>pp</value>\r\n                    </param>\r\n                    <param>\r\n                        <key>passwort</key>\r\n                        <value>pwd</value>\r\n                    </param>\r\n                </parameter>\r\n            </value>\r\n        </param>\r\n    </consumerConfigs>\r\n</org.emau.icmvc.ttp.notification.service.model.config.NotificationConfig>'
) ON CONFLICT (id) DO NOTHING;

RESET search_path;
