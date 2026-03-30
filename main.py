async def write_to_airtable(data: dict):
    api = Api(AIRTABLE_TOKEN)
    
    def fmt_date(d):
        # Пробуем разные форматы даты, которые вы можете прислать
        for fmt in ("%m.%d.%Y", "%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
            except:
                continue
        return datetime.now().strftime("%Y-%m-%d")

    try:
        if "Invoice_ID" in data:
            table = api.table(AIRTABLE_BASE_ID, TABLE_ORDERS)
            full_id = data.get("Invoice_ID", "")
            
            # Логика определения клиента по ID (Peto1910 -> Peto)
            client_match = re.match(r'^([a-zA-Zа-яА-Я]+)', full_id)
            client_name = client_match.group(1) if client_match else ""

            # ВНИМАНИЕ: Проверьте, что имена полей СЛЕВА в точности как в Airtable!
            record = {
                "Код Карго": str(full_id),
                "Клиент": str(client_name),
                "Дата": fmt_date(data.get("Date")),
                "Сумма (¥)": float(data.get("Sum_Client_CNY", 0)),
                "Курс Клиент": float(data.get("Client_Rate", 0)),
                "Курс Реал": float(data.get("Real_Rate", 0)),
                "Расход материалов (¥)": float(data.get("China_Logistics_CNY", 0))
            }
            
            # Пытаемся создать запись
            result = table.create(record, typecast=True)
            logger.info(f"Airtable Success: {result}")
            return f"✅ Заказ {full_id} успешно сохранен в Airtable!"

    except Exception as e:
        logger.error(f"Airtable Error: {e}")
        # Выводим конкретную ошибку в чат для диагностики
        return f"❌ Ошибка Airtable: {str(e)}"
    
    return "❌ Ошибка: Не найден Invoice_ID в сообщении."
