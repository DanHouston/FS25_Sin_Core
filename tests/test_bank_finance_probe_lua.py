"""The finance discovery command reads deployed schemas without booking money."""

from pathlib import Path
import unittest

from lupa.lua51 import LuaRuntime


MOD = Path(__file__).resolve().parents[1] / "mods/FS25_SiN_Server"


def runtime(server=False, admin=False):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        addModEventListener=function() end
        Class=function(class) return {__index=class} end
        Event={new=function(mt) return setmetatable({}, mt) end}
        InitEventClass=function() end
        logs={}
        Logging={info=function(format, ...) table.insert(logs, string.format(format, ...)) end}
        function forbidMutation() error('probe attempted a mutation') end
        MoneyType={register=forbidMutation, LAST_ID=29,
            OTHER={id=1, statistic='other', title='finance_other'}}
        FinanceStats={register=forbidMutation, saveToXMLFile=forbidMutation,
            writeStream=forbidMutation, attributes={
                {name='harvestIncome', title='finance_harvestIncome'},
                {name='other', title='finance_other'}}}
        FarmStats={addMoney=forbidMutation, saveToXMLFile=forbidMutation,
            STAT_ATTRIBUTES={{name='other', type='finance'}}}
        farmReads=0
        farm={money=1234, changeBalance=forbidMutation,
            getBalance=function() return 1234 end,
            stats={finances={other=-50}, financesHistory={{other=-25}}, addMoney=forbidMutation}}
        g_farmManager={getFarmById=function(_, id)
            farmReads=farmReads+1
            if id == 2 then return farm end
        end}
        InGameMenuStatisticsFrame={financeCategories={other='Miscellaneous'}}
        g_inGameMenu={pageStatistics={financeRows={{name='other', amount=-50}}}}
        sent=nil
        connection={getIsServer=function() return false end,
            sendEvent=function(_, event) sent=event end}
        g_client={getServerConnection=function() return connection end}
        """)
    for file in ("NetworkLocal.lua", "FinanceProbe.lua",
                 "events/SiNFarmUserPermissionsResponseEvent.lua",
                 "events/SiNFinanceProbeRequestEvent.lua"):
        lua.execute((MOD / file).read_text(encoding="utf-8"))
    lua.globals().serverSide = server
    lua.globals().isAdmin = admin
    lua.execute("""
        g_currentMission={getIsServer=function() return serverSide end,
            getIsClient=function() return not serverSide end, addMoney=forbidMutation,
            userManager={getUserIdByConnection=function(_, c)
                if c == connection then return 3 end
            end, getUserByUserId=function(_, id)
                if id == 3 then return {getIsMasterUser=function() return isAdmin end} end
            end}}
        """)
    return lua


class BankFinanceProbeLuaTests(unittest.TestCase):
    def test_probe_reads_native_categories_history_and_client_report_without_mutation(self):
        lua = runtime()
        result = lua.eval("FS25SiNServer:readBankFinanceProbe(2)")
        for text in ("readOnly=true", "statistic:other", "financesHistory", "financeRows", "farm.balance=1234"):
            self.assertIn(text, result)
        self.assertEqual(lua.eval("farm.money"), 1234)
        self.assertEqual(lua.eval("farm.stats.financesHistory[1].other"), -25)

    def test_probe_prioritizes_finance_schema_and_omits_gui_layout_noise(self):
        lua = runtime()
        lua.execute("""
            local page=g_inGameMenu.pageStatistics
            page.financesList={dataSource=page, delegate=page,
                absPosition={1,2}, anchors={0,0,0,0}, sections={{name='Misc'}}}
            for index=1,200 do
                page['categoryHeader'..index]={absPosition={1,2}, anchors={0,0,0,0}}
            end
            """)
        result = lua.eval("FS25SiNServer:readBankFinanceProbe(2)")
        self.assertIn("FinanceStats.attributes", result)
        self.assertIn("name:other", result)
        self.assertIn("financesList.dataSourceIsPage=true", result)
        self.assertIn("financesList.sections", result)
        self.assertIn("truncated=false", result)
        self.assertNotIn("categoryHeader", result)

    def test_client_command_requests_server_probe_without_server_console(self):
        lua = runtime()
        result = lua.eval("FS25SiNServer:consoleCommandBankFinance('2')")
        self.assertIn("server result will appear", result)
        self.assertEqual(lua.eval("sent.farmId"), 2)
        self.assertIn("Finance Probe", lua.eval("logs[1]"))

    def test_server_authenticates_admin_before_reading_requested_farm(self):
        lua = runtime(server=True, admin=True)
        lua.execute("SiNFinanceProbeRequestEvent.new(2):run(connection)")
        self.assertIn("side=server", lua.eval("sent.message"))
        self.assertIn("farm.balance=1234", lua.eval("sent.message"))

    def test_non_admin_does_not_receive_other_farm_finances(self):
        lua = runtime(server=True)
        lua.execute("SiNFinanceProbeRequestEvent.new(2):run(connection)")
        self.assertIn("denied", lua.eval("sent.message"))
        self.assertEqual(lua.eval("farmReads"), 0)

    def test_invalid_farm_ids_do_not_send_requests(self):
        lua = runtime()
        for value in ("0", "255", "1.5", "abc"):
            command = lua.eval("FS25SiNServer.consoleCommandBankFinance")
            self.assertIn("Usage:", command(lua.globals().FS25SiNServer, value))
        self.assertIsNone(lua.eval("sent"))

    def test_headless_server_and_large_unknown_schema_produce_bounded_response(self):
        lua = runtime(server=True, admin=True)
        lua.execute("""
            g_inGameMenu=nil
            for index=1,500 do
                FarmStats['field'..index]={title=string.rep('x', 1000)}
            end
            SiNFinanceProbeRequestEvent.new(2):run(connection)
            """)
        result = lua.eval("sent.message")
        self.assertLessEqual(len(result.encode()), 12200)
        self.assertIn("complete", result)


if __name__ == "__main__":
    unittest.main()
