DROP SCHEMA IF EXISTS `gpas` ;
CREATE DATABASE gpas COLLATE utf8mb4_unicode_ci;

USE gpas;

CREATE TABLE `domain` (
    `name` varchar(255) NOT NULL,
    `label` varchar(255) DEFAULT NULL,
    `alphabet` varchar(255) DEFAULT NULL,
    `comment` varchar(255) DEFAULT NULL,
    `generatorClass` varchar(255) DEFAULT NULL,
    `properties` varchar(1023) DEFAULT NULL,
    `create_timestamp` timestamp(3) NOT NULL DEFAULT current_timestamp(3),
    `update_timestamp` timestamp(3) NOT NULL DEFAULT current_timestamp(3),
    `expiration_properties` varchar(255) DEFAULT NULL,
    PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_bin;

CREATE TABLE `domain_parents` (
    `domain` varchar(255) NOT NULL,
    `parentDomain` varchar(255) NOT NULL,
    PRIMARY KEY (`domain`,`parentDomain`),
    KEY `FK_domain_parents_domain_2` (`parentDomain`),
    CONSTRAINT `FK_domain_parents_domain` FOREIGN KEY (`domain`) REFERENCES `domain` (`name`),
    CONSTRAINT `FK_domain_parents_domain_2` FOREIGN KEY (`parentDomain`) REFERENCES `domain` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_bin;

CREATE TABLE `psn` (
    `originalValue` varchar(255) NOT NULL,
    `pseudonym` varchar(255) NOT NULL,
    `domain` varchar(255) NOT NULL,
    `encoded_expiration_date` smallint DEFAULT NULL,
    PRIMARY KEY (`domain`,`originalValue`),
    UNIQUE KEY `domain_pseudonym` (`domain`,`pseudonym`),
    CONSTRAINT `FK_DOMAIN` FOREIGN KEY (`domain`) REFERENCES `domain` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_bin;

CREATE TABLE `mpsn` (
    `originalValue` varchar(255) NOT NULL,
    `pseudonym` varchar(255) NOT NULL,
    `domain` varchar(255) NOT NULL,
    `encoded_expiration_date` smallint DEFAULT NULL,
    PRIMARY KEY (`domain`,`originalValue`,`pseudonym`),
    UNIQUE KEY `domain_pseudonym` (`domain`,`pseudonym`),
    INDEX `domain_originalValue` (`domain`,`originalValue`),
    CONSTRAINT `FK_DOMAIN_MPSN` FOREIGN KEY (`domain`) REFERENCES `domain` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8 COLLATE=utf8_bin;

CREATE TABLE IF NOT EXISTS `stat_entry` (
    `STAT_ENTRY_ID` BIGINT(20) NOT NULL AUTO_INCREMENT,
    `ENTRYDATE` TIMESTAMP(3) NOT NULL DEFAULT current_timestamp(3),
    PRIMARY KEY (`STAT_ENTRY_ID`)
) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8;

CREATE TABLE IF NOT EXISTS `stat_value` (
    `stat_value_id` BIGINT(20) NULL DEFAULT NULL,
    `stat_value` BIGINT(20) NULL DEFAULT NULL,
    `stat_attr` VARCHAR(255) NULL DEFAULT NULL,
    INDEX `FK_stat_value_stat_value_id` (`stat_value_id` ASC),
    CONSTRAINT `FK_stat_value_stat_value_id`
    FOREIGN KEY (`stat_value_id` )
    REFERENCES `stat_entry` (`STAT_ENTRY_ID` )
) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8;

CREATE TABLE sequence (
    SEQ_NAME varchar(50) PRIMARY KEY NOT NULL,
    SEQ_COUNT decimal(38,0)
) ENGINE=InnoDB COLLATE=utf8mb4_unicode_ci;

DROP VIEW IF EXISTS `psn_domain_count`;
CREATE VIEW `psn_domain_count` AS
    SELECT
        CONCAT('pseudonyms_per_domain.', `d`.`name`) AS `attribut`,
        (COUNT(`p`.`pseudonym`) + COUNT(`m`.`pseudonym`)) AS `value`
    FROM
        `domain` `d`
        LEFT JOIN `psn` `p` ON `p`.`domain` = `d`.`name`
        LEFT JOIN `mpsn` `m` ON `m`.`domain` = `d`.`name`
    WHERE
        `d`.`name` != 'internal_anonymisation_domain'
    GROUP BY `d`.`name`;

DELIMITER $$
CREATE PROCEDURE `convert_to_multi_psn_domain`(
    IN in_domain VARCHAR(255)
)
BEGIN
    DECLARE conflict INT DEFAULT 0;
    DECLARE msg VARCHAR(255);

    -- prüfe ob domain existiert
    SELECT IF(COUNT(*),0,1) INTO conflict FROM domain WHERE name = in_domain;
    IF conflict THEN
        SET msg = CONCAT('Domain "', in_domain, '" not exists.');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
    END IF;

    -- prüfe ob domain bereits in multi-psn vorhanden
    SELECT COUNT(*) INTO conflict FROM (
                                           SELECT originalValue FROM mpsn WHERE domain = in_domain LIMIT 1
                                       ) t;
    IF conflict THEN
        SET msg = CONCAT('Domain "', in_domain, '" already exists in mpsn-table.');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
    END IF;

    START TRANSACTION;

    REPLACE INTO mpsn (originalValue, pseudonym, domain, encoded_expiration_date)
    SELECT originalValue, pseudonym, domain, encoded_expiration_date FROM psn WHERE domain = in_domain;

    DELETE FROM psn WHERE domain = in_domain;

    UPDATE domain
    SET properties = CONCAT(
            REGEXP_REPLACE(REGEXP_REPLACE(
                                   IFNULL(properties,''), ';?MULTI_PSN_DOMAIN=[^;]+', ''
                           ), '^;', ''),
            IF(IFNULL(properties,'')='' OR RIGHT(properties, 1)=';', '', ';'),
            'MULTI_PSN_DOMAIN=true;'
                     )
    WHERE name = in_domain;

    COMMIT;
END$$

CREATE PROCEDURE `convert_to_single_psn_domain`(
    IN in_domain VARCHAR(255)
)
BEGIN
    DECLARE conflict INT DEFAULT 0;
    DECLARE msg VARCHAR(255);

    -- prüfe ob domain existiert
    SELECT IF(COUNT(*),0,1) INTO conflict FROM domain WHERE name = in_domain;
    IF conflict THEN
        SET msg = CONCAT('Domain "', in_domain, '" not exists.');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
    END IF;

    -- prüfe ob domain bereits in single-psn vorhanden
    SELECT COUNT(*) INTO conflict FROM (
                                           SELECT originalValue FROM psn WHERE domain = in_domain LIMIT 1
                                       ) t;
    IF conflict THEN
        SET msg = CONCAT('Domain "', in_domain, '" already exists in psn-table.');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
    END IF;

    -- prüfe Konflikte mit mehrfachen originalValue
    SELECT COUNT(*) INTO conflict FROM (
                                           SELECT originalValue FROM mpsn WHERE domain = in_domain
                                           GROUP BY originalValue, domain HAVING COUNT(*) > 1 LIMIT 1
                                       ) t;
    IF conflict THEN
        SET msg = CONCAT('At least one originalValue is not unique in domain "', in_domain, '".');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = msg;
    END IF;

    START TRANSACTION;

    INSERT INTO psn (originalValue, pseudonym, domain, encoded_expiration_date)
    SELECT originalValue, pseudonym, domain, encoded_expiration_date FROM mpsn WHERE domain = in_domain;

    DELETE FROM mpsn WHERE domain = in_domain;

    UPDATE domain
    SET properties = REGEXP_REPLACE(properties, 'MULTI_PSN_DOMAIN=[^;]+', 'MULTI_PSN_DOMAIN=false')
    WHERE name = in_domain;

    COMMIT;
END$$
DELIMITER ;


CREATE USER 'gpas_user'@'%' IDENTIFIED BY 'gpas_password';
GRANT ALL ON gpas.* TO 'gpas_user'@'%';
