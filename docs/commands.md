!blog		{GET}(https://gatewaycorporate.org/api/blog/recent)[title:items.0.title,excerpt:items.0.excerpt,slug:items.0.slug] The most recent piece on our blog is titled "{title}". {excerpt} https://gatewaycorporate.org/blog/{slug}
!discord	https://discord.gg/UwZ8SaEShv
!forum		{GET}(https://gatewaycorporate.org/api/forum/stats)[boards:totals.boards,threads:totals.threads,posts:totals.posts] {threads} threads and {posts} posts across {boards} active boards: https://gatewaycorporate.org/forum
!irc		irc.techchat.net (#informationsuperhighway)	
!roll		{1..{query}}
!stoicism	{GET}(https://stoic.tekloon.net/stoic-quote)[author:data.author,quote:data.quote] "{quote}" - {author}
!twitch		https://twitch.tv/its_not_qwerty
!website	https://gatewaycorporate.org/
!wolfram	{GET}(https://api.wolframalpha.com/v1/result?i={query}&appid=VY3XG3AVGY)
!x		    https://x.com/Samuel_Roux_
!youtube	https://youtube.com/@apollyvision
!zen		{GET}(https://zenquotes.io/api/quotes)[quote:{0..49}.q] "{quote}"