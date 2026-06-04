"""
Asya Persona Data — Auto Expert Facts, Car Diagnostic Helpers, Expertise Knowledge
"""

import re
from typing import List, Dict, Optional, Tuple


# ── Car brand database ──────────────────────────────────────────────────────────

CAR_BRANDS = {
    "ACURA": {"country": "Япония", "parent": "Honda", "popular_models": ["MDX", "RDX", "TLX", "ILX"]},
    "ALFA ROMEO": {"country": "Италия", "parent": "Stellantis", "popular_models": ["Giulia", "Stelvio", "Tonale"]},
    "AUDI": {"country": "Германия", "parent": "Volkswagen Group", "popular_models": ["A3", "A4", "A6", "Q5", "Q7", "e-tron"]},
    "BMW": {"country": "Германия", "parent": "BMW Group", "popular_models": ["3 Series", "5 Series", "X3", "X5", "X7", "iX"]},
    "BYD": {"country": "Китай", "parent": "BYD Auto", "popular_models": ["Song", "Tang", "Han", "Seal", "Dolphin"]},
    "CADILLAC": {"country": "США", "parent": "General Motors", "popular_models": ["Escalade", "CT5", "XT5", "Lyriq"]},
    "CHANGAN": {"country": "Китай", "parent": "Changan Automobile", "popular_models": ["CS35 Plus", "CS55 Plus", "CS75", "UNI-V"]},
    "CHERY": {"country": "Китай", "parent": "Chery Automobile", "popular_models": ["Tiggo 4 Pro", "Tiggo 7 Pro", "Tiggo 8 Pro", "Arrizo 8"]},
    "CHEVROLET": {"country": "США", "parent": "General Motors", "popular_models": ["Camaro", "Corvette", "Tahoe", "Traverse", "Silverado"]},
    "CITROEN": {"country": "Франция", "parent": "Stellantis", "popular_models": ["C3", "C4", "C5 Aircross", "Berlingo"]},
    "DAEWOO": {"country": "Южная Корея", "parent": "GM Korea", "popular_models": ["Matiz", "Nexia", "Lanos"]},
    "DAIHATSU": {"country": "Япония", "parent": "Toyota", "popular_models": ["Rocky", "Taft", "Mira"]},
    "DATSUN": {"country": "Япония", "parent": "Nissan", "popular_models": ["GO", "on-DO", "mi-DO"]},
    "DODGE": {"country": "США", "parent": "Stellantis", "popular_models": ["Challenger", "Charger", "Durango", "Hornet"]},
    "EXEED": {"country": "Китай", "parent": "Chery", "popular_models": ["TXL", "VX", "LX"]},
    "FIAT": {"country": "Италия", "parent": "Stellantis", "popular_models": ["500", "Panda", "Tipo", "Ducato"]},
    "FORD": {"country": "США", "parent": "Ford Motor", "popular_models": ["Focus", "Mustang", "Explorer", "F-150", "Bronco", "Kuga"]},
    "GEELY": {"country": "Китай", "parent": "Geely Auto", "popular_models": ["Coolray", "Atlas Pro", "Monjaro", "Emgrand"]},
    "GENESIS": {"country": "Южная Корея", "parent": "Hyundai", "popular_models": ["G70", "G80", "GV70", "GV80"]},
    "GREAT WALL": {"country": "Китай", "parent": "GWM", "popular_models": ["Haval H6", "Haval Jolion", "Poer", "Tank 300"]},
    "HONDA": {"country": "Япония", "parent": "Honda Motor", "popular_models": ["Civic", "Accord", "CR-V", "HR-V", "Pilot"]},
    "HYUNDAI": {"country": "Южная Корея", "parent": "Hyundai Motor", "popular_models": ["Solaris", "Creta", "Tucson", "Santa Fe", "Elantra", "i30"]},
    "INFINITI": {"country": "Япония", "parent": "Nissan", "popular_models": ["Q50", "QX55", "QX60", "QX80"]},
    "JAGUAR": {"country": "Великобритания", "parent": "Tata Motors", "popular_models": ["F-Pace", "F-Type", "XE", "I-Pace"]},
    "JEEP": {"country": "США", "parent": "Stellantis", "popular_models": ["Wrangler", "Grand Cherokee", "Compass", "Cherokee"]},
    "KIA": {"country": "Южная Корея", "parent": "Hyundai Motor", "popular_models": ["Rio", "Ceed", "Sportage", "Sorento", "K5", "Seltos"]},
    "LADA (ВАЗ)": {"country": "Россия", "parent": "АвтоВАЗ", "popular_models": ["Granta", "Vesta", "Niva Travel", "Niva Legend", "Largus"]},
    "LAND ROVER": {"country": "Великобритания", "parent": "Tata Motors", "popular_models": ["Range Rover", "Defender", "Discovery", "Evoque"]},
    "LEXUS": {"country": "Япония", "parent": "Toyota", "popular_models": ["RX", "NX", "ES", "LX", "IS"]},
    "LINCOLN": {"country": "США", "parent": "Ford Motor", "popular_models": ["Navigator", "Aviator", "Corsair", "Nautilus"]},
    "MAZDA": {"country": "Япония", "parent": "Mazda Motor", "popular_models": ["3", "6", "CX-5", "CX-9", "MX-5", "CX-60"]},
    "MERCEDES-BENZ": {"country": "Германия", "parent": "Mercedes-Benz Group", "popular_models": ["C-Class", "E-Class", "S-Class", "GLC", "GLE", "EQS"]},
    "MINI": {"country": "Великобритания", "parent": "BMW Group", "popular_models": ["Cooper", "Countryman", "Clubman"]},
    "MITSUBISHI": {"country": "Япония", "parent": "Mitsubishi Motors", "popular_models": ["Outlander", "Pajero Sport", "Eclipse Cross", "L200"]},
    "NISSAN": {"country": "Япония", "parent": "Nissan Motor", "popular_models": ["Qashqai", "X-Trail", "Juke", "Patrol", "Almera"]},
    "OPEL": {"country": "Германия", "parent": "Stellantis", "popular_models": ["Astra", "Mokka", "Crossland", "Grandland", "Vivaro"]},
    "PEUGEOT": {"country": "Франция", "parent": "Stellantis", "popular_models": ["208", "308", "3008", "5008", "Partner"]},
    "PORSCHE": {"country": "Германия", "parent": "Volkswagen Group", "popular_models": ["911", "Cayenne", "Macan", "Taycan", "Panamera"]},
    "RENAULT": {"country": "Франция", "parent": "Renault Group", "popular_models": ["Duster", "Arkana", "Logan", "Sandero", "Kaptur"]},
    "ROLLS-ROYCE": {"country": "Великобритания", "parent": "BMW Group", "popular_models": ["Phantom", "Ghost", "Cullinan", "Spectre"]},
    "SKODA": {"country": "Чехия", "parent": "Volkswagen Group", "popular_models": ["Octavia", "Kodiaq", "Karoq", "Superb", "Rapid"]},
    "SUBARU": {"country": "Япония", "parent": "Subaru Corp", "popular_models": ["Forester", "Outback", "XV", "Impreza", "WRX"]},
    "SUZUKI": {"country": "Япония", "parent": "Suzuki Motor", "popular_models": ["Jimny", "Vitara", "S-Cross", "Swift"]},
    "TOYOTA": {"country": "Япония", "parent": "Toyota Motor", "popular_models": ["Camry", "RAV4", "Land Cruiser", "Corolla", "Highlander", "Prado"]},
    "VOLKSWAGEN": {"country": "Германия", "parent": "Volkswagen Group", "popular_models": ["Golf", "Tiguan", "Polo", "Touareg", "ID.4", "Passat"]},
    "VOLVO": {"country": "Швеция", "parent": "Geely", "popular_models": ["XC60", "XC90", "XC40", "S60", "V60", "EX90"]},
    "ZEEKR": {"country": "Китай", "parent": "Geely", "popular_models": ["001", "009", "X"]},
    "UAZ": {"country": "Россия", "parent": "УАЗ", "popular_models": ["Patriot", "Hunter", "Profi", "Pickup"]},
    "GAZ": {"country": "Россия", "parent": "Группа ГАЗ", "popular_models": ["ГАЗель", "Соболь", "Валдай"]},
    "HAVAL": {"country": "Китай", "parent": "Great Wall Motors", "popular_models": ["H6", "Jolion", "F7", "Dargo", "H9"]},
    "TANK": {"country": "Китай", "parent": "Great Wall Motors", "popular_models": ["300", "500", "700"]},
    "LI AUTO": {"country": "Китай", "parent": "Li Auto", "popular_models": ["L7", "L8", "L9", "MEGA"]},
    "NIO": {"country": "Китай", "parent": "NIO", "popular_models": ["ET7", "ES6", "ES8", "EC6"]},
    "XPENG": {"country": "Китай", "parent": "XPeng", "popular_models": ["P7", "G6", "G9", "X9"]},
    "RIVIAN": {"country": "США", "parent": "Rivian", "popular_models": ["R1T", "R1S"]},
    "LUCID": {"country": "США", "parent": "Lucid Motors", "popular_models": ["Air", "Gravity"]},
    "TESLA": {"country": "США", "parent": "Tesla Inc", "popular_models": ["Model 3", "Model Y", "Model S", "Model X", "Cybertruck"]},
}


# ── Common OBD-II error codes ───────────────────────────────────────────────────

OBD2_CODES = {
    "P0010": "Неисправность цепи управления клапаном фаз газораспределения (ряд 1)",
    "P0011": "Положение распредвала — опережение зажигания / производительность (ряд 1)",
    "P0012": "Положение распредвала — задержка зажигания (ряд 1)",
    "P0013": "Цепь управления клапаном фаз газораспределения (ряд 1)",
    "P0020": "Неисправность цепи управления клапаном фаз газораспределения (ряд 2)",
    "P0030": "Цепь управления нагревателем датчика кислорода (ряд 1, датчик 1)",
    "P0031": "Низкий уровень сигнала цепи управления нагревателем HO2S (ряд 1, датчик 1)",
    "P0032": "Высокий уровень сигнала цепи управления нагревателем HO2S (ряд 1, датчик 1)",
    "P0100": "Неисправность цепи датчика массового расхода воздуха (MAF)",
    "P0101": "Диапазон/производительность датчика массового расхода воздуха",
    "P0102": "Низкий уровень сигнала датчика массового расхода воздуха",
    "P0103": "Высокий уровень сигнала датчика массового расхода воздуха",
    "P0110": "Неисправность цепи датчика температуры впускного воздуха (IAT)",
    "P0115": "Неисправность цепи датчика температуры охлаждающей жидкости (ECT)",
    "P0120": "Неисправность цепи датчика положения дроссельной заслонки/педали",
    "P0128": "Термостат — температура охлаждающей жидкости ниже порога регулирования",
    "P0130": "Неисправность цепи датчика кислорода (ряд 1, датчик 1)",
    "P0131": "Низкое напряжение цепи датчика кислорода (ряд 1, датчик 1)",
    "P0140": "Нет активности цепи датчика кислорода (ряд 1, датчик 2)",
    "P0170": "Неисправность топливной коррекции (ряд 1)",
    "P0171": "Система слишком бедная (ряд 1)",
    "P0172": "Система слишком богатая (ряд 1)",
    "P0174": "Система слишком бедная (ряд 2)",
    "P0175": "Система слишком богатая (ряд 2)",
    "P0190": "Неисправность цепи датчика давления топлива в рампе",
    "P0217": "Перегрев двигателя",
    "P0218": "Перегрев коробки передач",
    "P0230": "Неисправность первичной цепи топливного насоса",
    "P0234": "Перегрузка турбокомпрессора/компрессора",
    "P0235": "Неисправность цепи датчика А турбокомпрессора",
    "P0299": "Недостаточная производительность турбокомпрессора/компрессора",
    "P0300": "Обнаружены пропуски зажигания (случайные/множественные цилиндры)",
    "P0301": "Обнаружены пропуски зажигания в цилиндре 1",
    "P0302": "Обнаружены пропуски зажигания в цилиндре 2",
    "P0303": "Обнаружены пропуски зажигания в цилиндре 3",
    "P0304": "Обнаружены пропуски зажигания в цилиндре 4",
    "P0305": "Обнаружены пропуски зажигания в цилиндре 5",
    "P0306": "Обнаружены пропуски зажигания в цилиндре 6",
    "P0315": "Система изменения фаз газораспределения не обучена",
    "P0335": "Неисправность цепи датчика положения коленвала",
    "P0336": "Диапазон/производительность цепи датчика положения коленвала",
    "P0340": "Неисправность цепи датчика положения распредвала (ряд 1)",
    "P0341": "Диапазон/производительность датчика положения распредвала",
    "P0351": "Неисправность первичной/вторичной цепи катушки зажигания A",
    "P0365": "Неисправность цепи датчика положения распредвала (ряд 2)",
    "P0400": "Неисправность системы рециркуляции отработавших газов (EGR)",
    "P0401": "Недостаточный поток рециркуляции отработавших газов",
    "P0403": "Неисправность цепи управления клапаном EGR",
    "P0420": "Эффективность катализатора ниже порога (ряд 1)",
    "P0421": "Эффективность катализатора ниже порога (ряд 1, прогрев)",
    "P0430": "Эффективность катализатора ниже порога (ряд 2)",
    "P0441": "Некорректный поток системы улавливания паров топлива (EVAP)",
    "P0442": "Утечка в системе EVAP (малая)",
    "P0455": "Утечка в системе EVAP (большая)",
    "P0480": "Неисправность цепи управления вентилятором охлаждения 1",
    "P0500": "Неисправность датчика скорости автомобиля",
    "P0504": "Корреляция выключателя стоп-сигнала A/B",
    "P0562": "Низкое напряжение системы",
    "P0563": "Высокое напряжение системы",
    "P0600": "Неисправность канала связи CAN (последовательный)",
    "P0601": "Ошибка контрольной суммы внутренней памяти ECM",
    "P0606": "Неисправность процессора ECM/PCM",
    "P0607": "Неисправность модуля управления — производительность",
    "P0700": "Неисправность системы управления коробкой передач (запрос от TCM)",
    "P0705": "Неисправность цепи датчика диапазона коробки передач",
    "P0715": "Неисправность цепи датчика частоты вращения турбины/входного вала",
    "P0720": "Неисправность цепи датчика скорости выходного вала",
    "P0725": "Неисправность цепи датчика оборотов двигателя (вход TCM)",
    "P0730": "Некорректное передаточное число",
    "P0731": "Некорректное передаточное число 1-й передачи",
    "P0732": "Некорректное передаточное число 2-й передачи",
    "P0740": "Неисправность системы муфты гидротрансформатора",
    "P0750": "Неисправность соленоида переключения A",
    "P0753": "Электрическая неисправность соленоида переключения A",
    "P0775": "Неисправность соленоида давления B",
    "P0800": "Неисправность системы управления раздаточной коробкой (запрос)",
    "P0850": "Неисправность цепи переключателя парковка/нейтраль (вход)",
    "P1101": "Диапазон/производительность датчика массового расхода воздуха",
    "P1400": "Неисправность цепи клапана рециркуляции отработавших газов (EA)",
    "P1420": "Неисправность вторичной цепи клапана подачи воздуха",
    "P1500": "Неисправность сигнала скорости автомобиля (间歇ный)",
    "P1600": "Потеря питания ECM",
    "P1700": "Неисправность коробки передач (производитель)",
    "P2100": "Неисправность цепи привода дроссельной заслонки (открыта)",
    "P2101": "Диапазон/производительность цепи привода дроссельной заслонки",
    "P2110": "Система управления дроссельной заслонкой — принудительный ограничитель оборотов",
    "P2122": "Низкий уровень сигнала датчика положения педали D",
    "P2127": "Низкий уровень сигнала датчика положения педали E",
    "P2135": "Корреляция напряжения датчика положения дроссельной заслонки/педали",
    "P2138": "Корреляция напряжения датчика положения педали D/E",
    "P2177": "Система слишком бедная (ряд 1, кроме холостого хода)",
    "P2187": "Система слишком бедная (ряд 1, холостой ход)",
    "P2196": "Сигнал датчика кислорода застрял на богатой (ряд 1, датчик 1)",
    "P2270": "Сигнал датчика кислорода застрял на бедной (ряд 1, датчик 2)",
    "P2400": "Неисправность цепи управления насосом обнаружения утечек EVAP",
    "P2500": "Низкий уровень сигнала лампы зарядки генератора",
    "P2502": "Диапазон/производительность системы зарядки",
    "P2534": "Низкое напряжение цепи выключателя зажигания",
    "P2600": "Низкое напряжение цепи управления насосом охлаждения",
    "P3400": "Система отключения цилиндров (ряд 1)",
    "P3497": "Система отключения цилиндров (ряд 2)",
    "U0001": "Неисправность высокоскоростной шины CAN",
    "U0073": "Шина CAN A отключена",
    "U0100": "Потеря связи с ECM/PCM A",
    "U0101": "Потеря связи с TCM",
    "U0121": "Потеря связи с ABS",
    "U0140": "Потеря связи с BCM",
    "U0151": "Потеря связи с модулем подушки безопасности",
    "U0155": "Потеря связи с комбинацией приборов",
    "U0300": "Несовместимость программного обеспечения внутреннего модуля управления",
}


# ── Common car problems by symptom ──────────────────────────────────────────────

SYMPTOM_DIAGNOSIS = {
    "engine_wont_start": {
        "symptoms": ["не заводится", "не запускается", "стартер крутит но не заводится", "машина не заводится"],
        "possible_causes": [
            "Разряжен аккумулятор",
            "Неисправность стартера",
            "Нет подачи топлива (топливный насос, фильтр, реле)",
            "Неисправность иммобилайзера",
            "Обрыв ремня ГРМ",
            "Неисправность датчика коленвала (ДПКВ)",
            "Нет искры (катушка, свечи, коммутатор)",
            "Загрязнение форсунок",
            "Низкая компрессия",
        ],
        "first_steps": [
            "Проверь напряжение аккумулятора (должно быть 12.4В+)",
            "Проверь, крутит ли стартер",
            "Слушай, гудит ли топливный насос при включении зажигания",
            "Проверь наличие искры на свече",
            "Считай коды ошибок OBD-II",
        ],
    },
    "engine_overheating": {
        "symptoms": ["перегревается", "кипит", "температура высокая", "стрелка в красной зоне", "перегрев"],
        "possible_causes": [
            "Низкий уровень охлаждающей жидкости",
            "Неисправность термостата",
            "Неисправность водяного насоса (помпы)",
            "Пробитая прокладка ГБЦ",
            "Засорён радиатор",
            "Неисправность вентилятора охлаждения",
            "Неисправность датчика температуры",
            "Воздушная пробка в системе охлаждения",
        ],
        "first_steps": [
            "Остановись и дай двигателю остыть (минимум 20-30 мин)",
            "Проверь уровень антифриза в расширительном бачке",
            "Осмотри на утечки под машиной",
            "Проверь, включается ли вентилятор",
            "Проверь, нагреваются ли оба патрубка радиатора (термостат)",
        ],
    },
    "check_engine": {
        "symptoms": ["чек", "check engine", "горит чек", "загорелся чек", "ошибка двигателя", "джеки чан"],
        "possible_causes": [
            "Неисправность датчика кислорода (лямбда-зонд)",
            "Снижение эффективности катализатора",
            "Пропуски зажигания (свечи, катушки)",
            "Неисправность датчика MAF",
            "Утечка в системе EVAP (крышка бензобака!)",
            "Неисправность датчика EGR",
            "Проблемы с топливной системой",
        ],
        "first_steps": [
            "Считай код ошибки OBD-II сканером",
            "Проверь, плотно ли закручена крышка бензобака",
            "Обрати внимание на поведение: троит, теряет мощность, повышенный расход?",
            "Если код P0420 — скорее всего катализатор",
            "Если код P0300 — пропуски, проверь свечи и катушки",
        ],
    },
    "vibration": {
        "symptoms": ["вибрация", "трясёт", "дрожит", "вибрирует", "троит"],
        "possible_causes": [
            "Пропуски зажигания (свечи, катушки, форсунки)",
            "Изношенные опоры двигателя",
            "Неисправность подушек КПП",
            "Дисбаланс колёс",
            "Изношенные ШРУСы",
            "Неисправность двухмассового маховика",
            "Загрязнение форсунок",
            "Неправильная работа системы зажигания",
        ],
        "first_steps": [
            "Определи, когда вибрация: на холостых, при движении, при торможении?",
            "Если на холостых — скорее всего опоры или пропуски",
            "Если при скорости 80-120 — балансировка колёс",
            "Если при разгоне — ШРУС",
            "Считай ошибки OBD-II",
        ],
    },
    "oil_consumption": {
        "symptoms": ["ест масло", "расход масла", "масложор", "уходит масло", "дымит"],
        "possible_causes": [
            "Износ маслосъёмных колпачков (МСК)",
            "Износ поршневых колец",
            "Утечки через прокладки (клапанной крышки, ГБЦ, поддона)",
            "Неисправность системы вентиляции картера (PCV)",
            "Турбина гонит масло (для турбомоторов)",
            "Деформация ГБЦ",
            "Неподходящее масло",
        ],
        "first_steps": [
            "Проверь уровень масла щупом",
            "Осмотри двигатель на подтёки масла",
            "Обрати внимание на цвет выхлопа: синий дым = масло",
            "Проверь свечи — масляный нагар?",
            "Замерь компрессию",
        ],
    },
    "transmission_problems": {
        "symptoms": ["коробка", "кпп", "автомат", "мкпп", "не переключает", "пинается", "робот", "вариатор"],
        "possible_causes": [
            "Низкий уровень/износ масла в АКПП",
            "Неисправность соленоидов",
            "Износ фрикционов",
            "Неисправность гидроблока",
            "Износ подшипников (МКПП)",
            "Износ сцепления (МКПП/робот)",
            "Износ ремня/конусов (вариатор)",
            "Неисправность мехатроника",
        ],
        "first_steps": [
            "Проверь уровень и цвет масла в коробке",
            "Обрати внимание: толчки при переключении, пробуксовки, шумы?",
            "Считай ошибки OBD-II / TCM",
            "Проверь адаптации коробки (АКПП/робот)",
        ],
    },
    "brake_problems": {
        "symptoms": ["тормоза", "скрипят", "биение при торможении", "мягкая педаль", "уводит"],
        "possible_causes": [
            "Износ тормозных колодок",
            "Деформация тормозных дисков",
            "Воздух в тормозной системе",
            "Неисправность тормозного цилиндра",
            "Подклинивание суппорта",
            "Износ тормозных шлангов",
            "Неисправность ABS",
        ],
        "first_steps": [
            "Проверь толщину тормозных колодок",
            "Осмотри тормозные диски на биение и борозды",
            "Проверь уровень тормозной жидкости",
            "Прокачай тормоза если педаль мягкая",
            "Проверь суппорты на подклинивание",
        ],
    },
    "suspension_noise": {
        "symptoms": ["стук", "грохот", "скрипит подвеска", "стучит на кочках", "гул"],
        "possible_causes": [
            "Износ стоек амортизаторов",
            "Износ шаровых опор",
            "Износ рулевых наконечников",
            "Износ ступичных подшипников (гул)",
            "Износ сайлентблоков",
            "Износ стабилизаторных втулок",
            "Ослабление креплений",
        ],
        "first_steps": [
            "Определи характер звука: стук, скрип, гул",
            "Стук на кочках — стойки/шаровые/сайлентблоки",
            "Гул, усиливающийся в поворотах — ступичный подшипник",
            "Скрип при повороте руля — рулевые наконечники/рулевая рейка",
            "Подними машину на подъёмнике и проверь люфты",
        ],
    },
    "electrical_problems": {
        "symptoms": ["электрика", "не горит", "предохранитель", "генератор", "аккумулятор разряжается", "короткое замыкание"],
        "possible_causes": [
            "Неисправность генератора",
            "Утечка тока",
            "Износ проводки",
            "Окисление контактов",
            "Неисправность блока BCM",
            "Неисправность реле",
        ],
        "first_steps": [
            "Замерь напряжение на АКБ: на заглушенной ~12.4В, на работающей 13.8-14.4В",
            "Если < 13.5В на работающей — генератор",
            "Замерь ток утечки (должно быть < 50мА)",
            "Проверь предохранители",
            "Проверь клеммы АКБ на окисление",
        ],
    },
}


# ── Helper functions ────────────────────────────────────────────────────────────

def lookup_obd2_code(code: str) -> Optional[str]:
    """Look up an OBD-II error code description."""
    code = code.upper().strip()
    if code in OBD2_CODES:
        return OBD2_CODES[code]
    # Try matching pattern PXXXX
    match = re.match(r"[PBUCE]\d{3,4}", code)
    if match:
        code_key = match.group(0)
        if len(code_key) == 4:
            code_key = code_key[0] + "0" + code_key[1:]
        return OBD2_CODES.get(code_key)
    return None


def identify_car_brand(text: str) -> Optional[str]:
    """Identify a car brand from text."""
    text_upper = text.upper()
    for brand in CAR_BRANDS:
        if brand in text_upper:
            return brand
    # Common abbreviations and aliases
    aliases = {
        "ВАЗ": "LADA (ВАЗ)", "ЛADA": "LADA (ВАЗ)", "ЛАДА": "LADA (ВАЗ)",
        "МЕРСЕДЕС": "MERCEDES-BENZ", "МЕРИН": "MERCEDES-BENZ",
        "ФОЛЬКСВАГЕН": "VOLKSWAGEN", "ФВ": "VOLKSWAGEN", "ВАГ": "VOLKSWAGEN",
        "БМВ": "BMW", "БЭХА": "BMW",
        "ЛАНД РОВЕР": "LAND ROVER", "РЕНДЖ РОВЕР": "LAND ROVER", "РЕНЖ": "LAND ROVER",
        "ПОРШ": "PORSCHE", "ПОРШЕ": "PORSCHE",
        "ШКОДА": "SKODA", "ШКОДУ": "SKODA",
        "ХЁНДАЙ": "HYUNDAI", "ХЕНДАЙ": "HYUNDAI", "ХЮНДАЙ": "HYUNDAI",
        "КИЯ": "KIA", "КИЮ": "KIA",
        "ТОЙОТА": "TOYOTA", "ТОЙОТУ": "TOYOTA",
        "МИЦУБИСИ": "MITSUBISHI", "МИЦУБИШИ": "MITSUBISHI",
        "СУБАРУ": "SUBARU",
        "ШЕВРОЛЕ": "CHEVROLET", "ШЕВРОЛЕТ": "CHEVROLET",
        "ФОРД": "FORD", "ФОРДА": "FORD",
        "РЕНО": "RENAULT", "РЕНОВ": "RENAULT",
        "ПЕЖО": "PEUGEOT",
        "СИТРОЕН": "CITROEN", "СИТРОЁН": "CITROEN",
        "ОПЕЛЬ": "OPEL", "ОПЕЛЯ": "OPEL",
        "УАЗ": "UAZ", "УАЗИК": "UAZ",
        "ГАЗЕЛЬ": "GAZ", "ГАЗ": "GAZ",
        "ХАВАЛ": "HAVAL",
        "ЧЕРИ": "CHERY",
        "ДЖИЛИ": "GEELY",
        "ЧАНГАН": "CHANGAN",
        "ТЕСЛА": "TESLA",
        "ПОРШЕ": "PORSCHE",
        "ЯГУАР": "JAGUAR",
        "ДЖИП": "JEEP",
        "ДАЦУН": "DATSUN",
    }
    for alias, brand in aliases.items():
        if alias in text_upper:
            return brand
    return None


def detect_symptoms(text: str) -> List[str]:
    """Detect car problem categories from user text."""
    text_lower = text.lower()
    detected = []
    for category, data in SYMPTOM_DIAGNOSIS.items():
        for symptom in data["symptoms"]:
            if symptom.lower() in text_lower:
                detected.append(category)
                break
    return detected


def detect_obd2_codes(text: str) -> List[str]:
    """Extract OBD-II codes from text."""
    pattern = r'[PBUCE]\d{3,4}'
    matches = re.findall(pattern, text.upper())
    # Normalize to PXXXX format
    normalized = []
    for m in matches:
        if len(m) == 4:
            m = m[0] + "0" + m[1:]
        if m in OBD2_CODES:
            normalized.append(m)
    return normalized


def is_part_number(text: str) -> bool:
    """Check if text looks like a part/article number (OEM number)."""
    text = text.strip()
    # Common OEM patterns: XXXXXX-XXXXX, XXX XXX XXXX, etc.
    patterns = [
        r'^\d{4,10}$',                          # Just digits (4-10)
        r'^[A-Z]{2,3}\d{4,8}$',                 # Letters+digits (e.g. WA6166)
        r'^[A-Z]{1,3}\d{3,6}[A-Z]?\d?$',        # e.g. VAG, BMW part numbers
        r'^\d{3,6}[A-Z]{1,3}\d{0,4}$',          # Digits+letters
        r'^[A-Z0-9]{4,15}[-\s][A-Z0-9]{3,15}$', # With separator
        r'^[A-Z]{2}\d{9,12}$',                   # VIN-like fragment
    ]
    for pattern in patterns:
        if re.match(pattern, text.upper()):
            return True
    return False


def extract_part_numbers(text: str) -> List[str]:
    """Extract possible part numbers from text."""
    words = text.replace(',', ' ').replace('.', ' ').split()
    parts = []
    for word in words:
        if is_part_number(word):
            parts.append(word.upper())
    return parts


def get_brand_info(brand: str) -> Optional[Dict]:
    """Get brand information by name."""
    return CAR_BRANDS.get(brand.upper())


def build_diagnostic_context(text: str) -> str:
    """Build additional context for AI when user describes a car problem."""
    context_parts = []

    # Detect car brand
    brand = identify_car_brand(text)
    if brand:
        info = get_brand_info(brand)
        if info:
            context_parts.append(f"Марка авто: {brand} ({info['country']}, холдинг: {info['parent']})")

    # Detect symptoms
    symptoms = detect_symptoms(text)
    if symptoms:
        for symptom_cat in symptoms:
            data = SYMPTOM_DIAGNOSIS[symptom_cat]
            context_parts.append(f"Категория проблемы: {symptom_cat}")
            context_parts.append(f"Возможные причины: {', '.join(data['possible_causes'][:5])}")
            context_parts.append(f"Первые шаги: {', '.join(data['first_steps'][:3])}")

    # Detect OBD-II codes
    codes = detect_obd2_codes(text)
    if codes:
        for code in codes:
            desc = lookup_obd2_code(code)
            if desc:
                context_parts.append(f"Код ошибки {code}: {desc}")

    # Detect part numbers
    parts = extract_part_numbers(text)
    if parts:
        context_parts.append(f"Артикулы запчастей в запросе: {', '.join(parts)}")

    if context_parts:
        return "\n".join(context_parts)
    return ""


# ── Asya's signature phrases for different contexts ─────────────────────────────

ASYA_PHRASES = {
    "greeting": [
        "Привет! 😊 Ася тут.",
        "Хей! Как дела?",
        "О, привет! Рад(а) тебя видеть!",
        "Привет! Кофе уже пью, можно общаться ☕",
    ],
    "diagnostic_start": [
        "Так, давай разберёмся. Расскажи подробнее, что происходит?",
        "Понял, сейчас подумаем. Что именно случилось?",
        "Блин, неприятно. Давай разбираться — опиши симптомы.",
    ],
    "part_search": [
        "Сейчас проверю, минуточку...",
        "Ищу, подожди чуть-чуть!",
        "Сейчас найду, секундочку.",
    ],
    "news_comment": [
        "Ого, вот это да!",
        "Слушай, а это интересно...",
        "Между нами говоря — это заслуживает внимания.",
    ],
    "thinking": [
        "Сейчас подумаю...",
        "Дай секунду...",
        "Минутку, анализирую...",
    ],
}
